"""Async MQTT client for the Ampio DB-object protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import aiomqtt

from . import _protocol
from .const import (
    DETAILS_REQUEST_PAYLOAD,
    DEVICES_REQUEST_PAYLOAD,
    GROUP_DEVICES_REQUEST_PAYLOAD,
    GROUPS_REQUEST_PAYLOAD,
    LOCATIONS_REQUEST_PAYLOAD,
    RAW_INPUT_WILDCARDS,
    _INPUT_CHANNEL_PREFIX,
    classify_input,
    classify_object,
    config_request_topic,
    data_request_topic,
    details_response_topic,
    devices_response_topic,
    group_devices_response_topic,
    groups_response_topic,
    info_request_topic,
    info_response_topic,
    locations_response_topic,
    ob_state_wildcard,
    states_request_topic,
    states_response_topic,
)
from .errors import AmpioAuthError, AmpioConnectionError
from .models import (
    AmpioModule,
    AmpioObject,
    AmpioServerInfo,
    AmpioState,
    ConnectionStats,
)
from .rooms import join_rooms

_LOGGER = logging.getLogger(__name__)

_RECONNECT_BACKOFF_MAX = 60.0

ObjectListener = Callable[[AmpioObject], None]
AvailabilityListener = Callable[[bool], None]


class AmpioClient:
    """Maintains a connection to the Ampio broker and tracks object state."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        *,
        reconnect_interval: float = 5.0,
    ) -> None:
        """Initialize the client. `username` also namespaces the MQTT topics."""
        self._host = host
        self._port = port
        self._username = username or ""
        self._password = password
        self._reconnect_interval = reconnect_interval
        # Stable client identifier; reusing the same id across reconnects keeps
        # the broker from seeing parallel "ghost" sessions while the previous
        # one expires.
        self._client_id = f"ampio_mqtt_{uuid.uuid4().hex}"

        self.state = AmpioState()
        self._object_listeners: list[ObjectListener] = []
        self._availability_listeners: list[AvailabilityListener] = []

        # Raw-channel bridge: maps a decoded channel topic to the Designer
        # object that owns it. Key is (module.mac, prefix, channel) -> object_id.
        self._input_index: dict[tuple[int, str, int], int] = {}
        # Object ids for which a raw-channel message has actually arrived. Once
        # an input is "raw-proven", the faster raw stream is authoritative and
        # its per-object echoes are suppressed; an input never seen on the raw
        # path (e.g. an M-SERV-internal object) keeps its per-object updates.
        self._raw_seen_ids: set[int] = set()

        self._client: aiomqtt.Client | None = None
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._auth_failed = asyncio.Event()
        self._details_received = asyncio.Event()
        self._devices_received = asyncio.Event()
        self._states_received = asyncio.Event()
        self._info_received = asyncio.Event()
        self._groups_received = asyncio.Event()
        self._group_devices_received = asyncio.Event()
        self._locations_received = asyncio.Event()

        # Last raw payloads as the broker sent them - retained for downstream
        # diagnostics so a tester report can include the actual JSON the
        # M-SERV emitted, without the consumer having to re-derive it.
        self.last_devices_payload: str | None = None
        self.last_details_payload: str | None = None
        self.last_info_payload: str | None = None
        self.last_groups_payload: str | None = None
        self.last_group_devices_payload: str | None = None
        self.last_locations_payload: str | None = None

        # Connection liveness counters surfaced as `client.stats`.
        self.stats = ConnectionStats()

        self._auth_error_message: str | None = None
        self._available = False
        self._stop = False

    # --- public API -------------------------------------------------------

    @property
    def objects(self) -> dict[int, AmpioObject]:
        """All known objects keyed by id."""
        return self.state.objects

    @property
    def modules(self) -> dict[int, AmpioModule]:
        """All known physical modules keyed by id."""
        return self.state.modules

    @property
    def server_info(self) -> AmpioServerInfo | None:
        """The Ampio M-SERV self-reported info, if discovered."""
        return self.state.server_info

    @property
    def mserv_id(self) -> int | None:
        """Resolve the module id of the M-SERV server.

        Prefers cross-validating the server's self-reported mac against each
        module's mac_global/mac; falls back to the unique module whose
        typ_urzadzenia is 10 (M-SERV-s).
        """
        info = self.state.server_info
        if info is not None and info.mac is not None:
            for mid, mod in self.state.modules.items():
                if info.mac in (mod.mac_global, mod.mac):
                    return mid
        candidates = [mid for mid, mod in self.state.modules.items() if mod.type == 10]
        if len(candidates) == 1:
            return candidates[0]
        return None

    @property
    def sensors(self) -> dict[int, AmpioObject]:
        """Objects classified as sensors."""
        return self.state.sensors

    @property
    def available(self) -> bool:
        """Whether the broker connection is up."""
        return self._available

    def add_object_listener(self, listener: ObjectListener) -> Callable[[], None]:
        """Register a callback invoked on every object update (state/metadata)."""
        self._object_listeners.append(listener)
        return lambda: self._object_listeners.remove(listener)

    def add_availability_listener(
        self, listener: AvailabilityListener
    ) -> Callable[[], None]:
        """Register a callback invoked when connection availability changes."""
        self._availability_listeners.append(listener)
        return lambda: self._availability_listeners.remove(listener)

    @staticmethod
    async def test_connection(
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        *,
        info_timeout: float = 5.0,
    ) -> AmpioServerInfo:
        """Connect, request the server info, and return it.

        Raises ``AmpioAuthError`` on credential rejection, ``AmpioConnectionError``
        on any other connection failure. If the connection succeeds but the
        server info reply does not arrive within ``info_timeout`` (restricted
        accounts, slow broker), returns an empty ``AmpioServerInfo`` rather
        than raising - the caller can decide whether identity is required.
        """
        user = username or ""
        info_topic = info_response_topic(user)
        try:
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=username,
                password=password,
                identifier=f"ampio_mqtt_test_{uuid.uuid4().hex}",
                timeout=10,
            ) as client:
                await client.subscribe(info_topic)
                await client.publish(info_request_topic(user), b"")
                try:
                    async with asyncio.timeout(info_timeout):
                        async for message in client.messages:
                            if str(message.topic) != info_topic:
                                continue
                            return _protocol.parse_server_info(
                                _decode_payload(message.payload)
                            )
                except TimeoutError:
                    return AmpioServerInfo()
        except aiomqtt.MqttError as err:
            if _protocol.is_auth_error(err):
                raise AmpioAuthError(str(err)) from err
            raise AmpioConnectionError(str(err)) from err
        return AmpioServerInfo()

    async def start(
        self, *, timeout: float = 15.0, discovery_timeout: float = 8.0
    ) -> None:
        """Start the connection, wait for connect and initial discovery.

        After connecting, waits up to `discovery_timeout` for the initial
        object and module lists so module names are known before entities are
        created. Restricted accounts may never receive these; the wait then
        simply times out and discovery continues opportunistically.
        """
        self._stop = False
        self._auth_failed.clear()
        self._auth_error_message = None
        self._runner = asyncio.create_task(self._run())
        connected_task = asyncio.create_task(self._connected.wait())
        auth_failed_task = asyncio.create_task(self._auth_failed.wait())
        try:
            done, _ = await asyncio.wait(
                {connected_task, auth_failed_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (connected_task, auth_failed_task):
                if not task.done():
                    task.cancel()
        if not done:
            await self.stop()
            raise AmpioConnectionError("Timed out connecting to Ampio")
        if self._auth_failed.is_set():
            await self.stop()
            raise AmpioAuthError(
                self._auth_error_message or "Authentication rejected by Ampio broker"
            )

        waiters = [
            asyncio.create_task(self._details_received.wait()),
            asyncio.create_task(self._devices_received.wait()),
            asyncio.create_task(self._states_received.wait()),
            asyncio.create_task(self._info_received.wait()),
        ]
        _, pending = await asyncio.wait(waiters, timeout=discovery_timeout)
        for task in pending:
            task.cancel()

    async def stop(self) -> None:
        """Stop the connection."""
        self._stop = True
        if self._runner:
            self._runner.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None

    async def request_details(self) -> None:
        """Ask the server for the devicesDetails object list."""
        await self._publish_config(DETAILS_REQUEST_PAYLOAD)

    async def request_devices(self) -> None:
        """Ask the server for the physical module list."""
        await self._publish_config(DEVICES_REQUEST_PAYLOAD)

    async def request_states(self) -> None:
        """Ask the server for a snapshot of all current object states."""
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        await self._client.publish(states_request_topic(self._username), b"")

    async def request_info(self) -> None:
        """Ask the server for its own info (version, mac, local IP, ...)."""
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        await self._client.publish(info_request_topic(self._username), b"")

    async def fetch_rooms(self, timeout: float = 5.0) -> dict[int, str]:
        """Return ``{ampio_object_id: room_name}`` for objects assigned to a room.

        Publishes the ``groups`` and ``group_devices`` keywords to
        ``ampio/control/<user>/data`` and awaits both responses on
        ``ampio/fromDB/<user>/data/<keyword>``. Joins them in memory; objects
        assigned to multiple groups map to the first room encountered (Home
        Assistant allows one area per device).

        Requires ``start()`` to have completed. Raises ``AmpioConnectionError``
        if the broker is not connected or either response does not arrive
        within ``timeout``.
        """
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        self._groups_received.clear()
        self._group_devices_received.clear()
        self.last_groups_payload = None
        self.last_group_devices_payload = None
        await self._publish_data(GROUPS_REQUEST_PAYLOAD)
        await self._publish_data(GROUP_DEVICES_REQUEST_PAYLOAD)
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(
                    self._groups_received.wait(),
                    self._group_devices_received.wait(),
                )
        except TimeoutError as err:
            raise AmpioConnectionError(
                "Timed out fetching room map from Ampio broker"
            ) from err
        return join_rooms(
            _safe_json_object(self.last_groups_payload),
            _safe_json_object(self.last_group_devices_payload),
        )

    async def fetch_locations(self, timeout: float = 5.0) -> dict[int, str]:
        """Return ``{location_id: name}`` for the Designer "Location" markers.

        The location is the user-editable per-output marker visible in the
        Designer's "Lokalizacja" column (e.g. ``Salon``, ``Kuchnia``, ...).
        It is **per-output**, not per-module: each module's outputs can be
        assigned to different locations, and the per-output assignment lives
        in the device's CAN-resident description table (not exposed via
        MQTT - see the comment in `_run` and the integration's docs for
        the RPC route that would resolve it).

        This method returns only the *name table* - the integer ID -> human
        label mapping the Designer uses to populate its dropdown. A consumer
        that does have a way to learn the per-output integer can resolve it
        through this dict. Without that, the table is still useful in
        diagnostics ("which location ids does this M-SERV define?").

        Publishes ``locations`` to ``ampio/control/<user>/config`` and awaits
        the response on ``ampio/fromDB/<user>/config/locations``. Requires
        ``start()`` to have completed. Raises ``AmpioConnectionError`` if the
        broker is not connected or the response does not arrive within
        ``timeout``.
        """
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        self._locations_received.clear()
        self.last_locations_payload = None
        await self._client.publish(
            config_request_topic(self._username),
            LOCATIONS_REQUEST_PAYLOAD.encode(),
        )
        try:
            async with asyncio.timeout(timeout):
                await self._locations_received.wait()
        except TimeoutError as err:
            raise AmpioConnectionError(
                "Timed out fetching locations table from Ampio broker"
            ) from err
        data = _safe_json_object(self.last_locations_payload)
        out: dict[int, str] = {}
        for item in data.get("List", []):
            if not isinstance(item, dict):
                continue
            lid = item.get("id")
            name = item.get("opis_menu")
            if isinstance(lid, int) and isinstance(name, str) and name:
                out[lid] = name
        return out

    async def _publish_data(self, keyword: str) -> None:
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        await self._client.publish(data_request_topic(self._username), keyword.encode())

    def _feed_message(self, topic: str, payload: str | bytes) -> None:
        """Inject a message directly into the routing logic.

        Private entry point used by the library's own tests; the real broker
        drives the same logic through `_run`.
        """
        self._dispatch(topic, _decode_payload(payload))

    async def _publish_config(self, keyword: str) -> None:
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        await self._client.publish(
            config_request_topic(self._username), keyword.encode()
        )

    # --- internal ---------------------------------------------------------

    async def _run(self) -> None:
        user = self._username
        attempt = 0
        while not self._stop:
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    identifier=self._client_id,
                    timeout=10,
                ) as client:
                    self._client = client
                    await client.subscribe(details_response_topic(user))
                    await client.subscribe(devices_response_topic(user))
                    await client.subscribe(states_response_topic(user))
                    await client.subscribe(info_response_topic(user))
                    await client.subscribe(groups_response_topic(user))
                    await client.subscribe(group_devices_response_topic(user))
                    await client.subscribe(locations_response_topic(user))
                    await client.subscribe(ob_state_wildcard(user))
                    for wildcard in RAW_INPUT_WILDCARDS:
                        await client.subscribe(wildcard)
                    if self.stats.started_at is None:
                        self.stats.started_at = time.time()
                    else:
                        self.stats.reconnect_count += 1
                    self._set_available(True)
                    self._connected.set()
                    attempt = 0
                    await self.request_devices()
                    await self.request_details()
                    await self.request_states()
                    await self.request_info()
                    async for message in client.messages:
                        self._dispatch(
                            str(message.topic), _decode_payload(message.payload)
                        )
            except aiomqtt.MqttError as err:
                self.stats.last_error = str(err)
                if _protocol.is_auth_error(err):
                    # Reconnecting will not help; surface to start() and stop.
                    self._auth_error_message = str(err)
                    self._auth_failed.set()
                    self._stop = True
                else:
                    _LOGGER.debug("Ampio MQTT connection error: %s", err)
            finally:
                self._client = None
                self._set_available(False)
            if not self._stop:
                await asyncio.sleep(self._backoff_seconds(attempt))
                attempt += 1

    def _backoff_seconds(self, attempt: int) -> float:
        """Capped exponential backoff with jitter, in seconds.

        Caps so a long outage with many concurrent installs does not
        thunder-herd the broker on recovery.
        """
        base = self._reconnect_interval
        capped = min(_RECONNECT_BACKOFF_MAX, base * (2.0**attempt))
        return float(capped + random.uniform(0.0, base))

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        for listener in list(self._availability_listeners):
            listener(available)

    def _dispatch(self, topic: str, payload: str) -> None:
        """Route a received MQTT message to the appropriate handler."""
        self.stats.last_message_at = time.time()
        if topic == details_response_topic(self._username):
            self._handle_details(payload)
        elif topic == devices_response_topic(self._username):
            self._handle_devices(payload)
        elif topic == states_response_topic(self._username):
            self._handle_states_snapshot(payload)
        elif topic == info_response_topic(self._username):
            self._handle_info(payload)
        elif topic == groups_response_topic(self._username):
            self.last_groups_payload = payload
            self._groups_received.set()
        elif topic == group_devices_response_topic(self._username):
            self.last_group_devices_payload = payload
            self._group_devices_received.set()
        elif topic == locations_response_topic(self._username):
            self.last_locations_payload = payload
            self._locations_received.set()
        elif topic.endswith("/state") and "/ob/" in topic:
            self._handle_state(topic, payload)
        elif topic.startswith("ampio/from/") and "/state/" in topic:
            self._handle_raw_channel(topic, payload)

    def _handle_details(self, payload: str) -> None:
        self.last_details_payload = payload
        items = _protocol.parse_details(payload)
        if items is None:
            _LOGGER.warning("Could not parse Ampio devicesDetails")
            return
        for meta in items:
            obj = self.state.objects.get(meta.id) or AmpioObject(id=meta.id)
            obj.device_id = meta.device_id
            obj.typ_komponentu = meta.typ_komponentu
            obj.name = meta.name or obj.name
            obj.interpretacja = meta.interpretacja
            obj.funkcja = meta.funkcja
            obj.leaf_id = meta.leaf_id
            obj.group_ids = meta.group_ids
            obj.kind = classify_object(meta.typ_komponentu, meta.interpretacja)
            obj.input_kind = classify_input(meta.typ_komponentu, meta.interpretacja)
            if obj.value is None and meta.stan_json is not None:
                self._apply_stan_json(obj, meta.stan_json)
            self.state.objects[meta.id] = obj
            self._notify(obj)
        self._rebuild_input_index()
        self._details_received.set()

    def _handle_devices(self, payload: str) -> None:
        self.last_devices_payload = payload
        modules = _protocol.parse_devices(payload)
        if modules is None:
            _LOGGER.warning("Could not parse Ampio devices list")
            return
        for module in modules:
            previous = self.state.modules.get(module.id)
            if previous is not None:
                module.last_seen = previous.last_seen
            self.state.modules[module.id] = module
        self._rebuild_input_index()
        self._devices_received.set()

    def _handle_info(self, payload: str) -> None:
        self.last_info_payload = payload
        self.state.server_info = _protocol.parse_server_info(payload)
        self._info_received.set()

    def _handle_states_snapshot(self, payload: str) -> None:
        entries = _protocol.parse_states_snapshot(payload)
        if entries is None:
            _LOGGER.warning("Could not parse Ampio states snapshot")
            return
        for entry in entries:
            obj = self.state.objects.get(entry.id)
            if obj is None:
                # Metadata not yet known (e.g. snapshot arrived before details).
                obj = AmpioObject(id=entry.id, kind=classify_object(None, None))
                self.state.objects[entry.id] = obj
            if obj.value is None and entry.stan_json is not None:
                self._apply_stan_json(obj, entry.stan_json)
            self._notify(obj)
        self._states_received.set()

    def _handle_state(self, topic: str, payload: str) -> None:
        update = _protocol.parse_state_message(topic, payload)
        if update is None:
            return
        if update.id in self._raw_seen_ids:
            # The faster raw-channel path is authoritative for this input; drop
            # the slower per-object echo to avoid a double notify and a stale
            # echo clobbering a fresh raw edge.
            return
        obj = self.state.objects.get(update.id)
        if obj is None:
            # No metadata yet (e.g. restricted account) -> generic sensor.
            obj = AmpioObject(id=update.id, kind=classify_object(None, None))
            self.state.objects[update.id] = obj
        obj.value = update.value
        self._touch_module(obj.device_id, update.on_ms)
        self._notify(obj)

    def _rebuild_input_index(self) -> None:
        """Rebuild the (mac, prefix, channel) -> object_id routing table.

        Keyed on the module's effective bus address (`mac`, the Designer
        override) - never `mac_global`, which diverges from the raw-topic MAC on
        replaced modules. Only bridgeable input types with a known channel and a
        known module mac are indexed.
        """
        index: dict[tuple[int, str, int], int] = {}
        for obj in self.state.objects.values():
            prefix = _INPUT_CHANNEL_PREFIX.get(obj.typ_komponentu or "")
            if prefix is None or obj.funkcja is None or obj.device_id is None:
                continue
            module = self.state.modules.get(obj.device_id)
            if module is None or module.mac is None:
                continue
            index[(module.mac, prefix, obj.funkcja)] = obj.id
        self._input_index = index

    def _handle_raw_channel(self, topic: str, payload: str) -> None:
        key = _protocol.parse_raw_channel_topic(topic)
        if key is None:
            return
        oid = self._input_index.get(key)
        if oid is None:
            return  # channel has no exposed Designer object - ignore
        obj = self.state.objects.get(oid)
        if obj is None:
            return
        self._raw_seen_ids.add(oid)
        obj.value = payload.strip()
        self._touch_module(obj.device_id, None)
        self._notify(obj)

    def _apply_stan_json(self, obj: AmpioObject, stan_json: str) -> None:
        """Seed `obj.value` from `stan_json` and bump the module's last_seen."""
        seed = _protocol.parse_stan_json(stan_json)
        if seed is None:
            return
        if seed.value is not None:
            obj.value = seed.value
        self._touch_module(obj.device_id, seed.on_ms)

    def _touch_module(self, module_id: int | None, on_ms: int | float | None) -> None:
        """Mark the module as having reported now (or at `on_ms`, server time)."""
        if module_id is None:
            return
        module = self.state.modules.get(module_id)
        if module is None:
            return
        if on_ms is not None and on_ms > 0:
            ts = float(on_ms) / 1000.0
        else:
            ts = time.time()
        if module.last_seen is None or ts > module.last_seen:
            module.last_seen = ts

    def _notify(self, obj: AmpioObject) -> None:
        for listener in list(self._object_listeners):
            listener(obj)


def _decode_payload(payload: object) -> str:
    """Coerce an aiomqtt payload (`str | bytes | bytearray | None`) to text."""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", "replace")
    if isinstance(payload, str):
        return payload
    return ""


def _safe_json_object(text: str | None) -> dict[str, Any]:
    """Parse `text` as a JSON object; return an empty dict on any failure."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
