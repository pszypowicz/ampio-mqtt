"""Async MQTT client for the Ampio DB-object protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import aiomqtt

from .const import (
    DETAILS_REQUEST_PAYLOAD,
    DEVICES_REQUEST_PAYLOAD,
    classify_object,
    config_request_topic,
    details_response_topic,
    devices_response_topic,
    info_request_topic,
    info_response_topic,
    ob_state_wildcard,
    states_request_topic,
    states_response_topic,
)
from .device_types import module_model
from .models import AmpioModule, AmpioObject, AmpioServerInfo, AmpioState

_LOGGER = logging.getLogger(__name__)

ObjectListener = Callable[[AmpioObject], None]
AvailabilityListener = Callable[[bool], None]


class AmpioError(Exception):
    """Base error."""


class AmpioConnectionError(AmpioError):
    """Raised when the broker connection fails for non-auth reasons."""


class AmpioAuthError(AmpioConnectionError):
    """Raised when the broker rejects the credentials."""


# CONNACK return codes / reason strings that indicate an auth failure rather
# than a network or transport problem. aiomqtt surfaces the broker text in the
# `MqttError` message; matching is heuristic but covers MQTT 3.1.1 (rc 4/5) and
# MQTT 5 (`not authorized`, `bad user name or password`).
_AUTH_ERROR_MARKERS = (
    "not authorized",
    "bad user name",
    "bad username",
    "unauthorized",
    "rc=4",
    "rc=5",
    "[code:4]",
    "[code:5]",
    "[code:134]",
    "[code:135]",
)


def _is_auth_error(err: aiomqtt.MqttError) -> bool:
    """Return True if the MQTT error looks like an authentication failure."""
    msg = str(err).lower()
    return any(marker in msg for marker in _AUTH_ERROR_MARKERS)


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
        self._client_id = f"aioampio_{uuid.uuid4().hex}"

        self.state = AmpioState()
        self._object_listeners: list[ObjectListener] = []
        self._availability_listeners: list[AvailabilityListener] = []

        self._client: aiomqtt.Client | None = None
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._auth_failed = asyncio.Event()
        self._details_received = asyncio.Event()
        self._devices_received = asyncio.Event()
        self._states_received = asyncio.Event()
        self._info_received = asyncio.Event()
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
                identifier=f"aioampio_test_{uuid.uuid4().hex}",
                timeout=10,
            ) as client:
                await client.subscribe(info_topic)
                await client.publish(info_request_topic(user), b"")
                try:
                    async with asyncio.timeout(info_timeout):
                        async for message in client.messages:
                            if str(message.topic) != info_topic:
                                continue
                            payload = message.payload
                            if isinstance(payload, bytes):
                                payload = payload.decode("utf-8", "replace")
                            return _parse_server_info(payload)
                except TimeoutError:
                    return AmpioServerInfo()
        except aiomqtt.MqttError as err:
            if _is_auth_error(err):
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
            done, pending = await asyncio.wait(
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

    def feed_message(self, topic: str, payload: str | bytes) -> None:
        """Inject a message directly into the routing logic.

        Public entry point intended for tests; the real broker drives the
        same logic through `_run`.
        """
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        self._dispatch(topic, payload)

    async def _publish_config(self, keyword: str) -> None:
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        await self._client.publish(
            config_request_topic(self._username), keyword.encode()
        )

    # --- internal ---------------------------------------------------------

    async def _run(self) -> None:
        user = self._username
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
                    await client.subscribe(ob_state_wildcard(user))
                    self._set_available(True)
                    self._connected.set()
                    await self.request_devices()
                    await self.request_details()
                    await self.request_states()
                    await self.request_info()
                    async for message in client.messages:
                        payload = message.payload
                        if isinstance(payload, bytes):
                            payload = payload.decode("utf-8", "replace")
                        self._dispatch(str(message.topic), payload)
            except aiomqtt.MqttError as err:
                if _is_auth_error(err):
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
                await asyncio.sleep(self._reconnect_interval)

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        for listener in list(self._availability_listeners):
            listener(available)

    def _dispatch(self, topic: str, payload: str) -> None:
        """Route a received MQTT message to the appropriate handler."""
        if topic == details_response_topic(self._username):
            self._handle_details(payload)
        elif topic == devices_response_topic(self._username):
            self._handle_devices(payload)
        elif topic == states_response_topic(self._username):
            self._handle_states_snapshot(payload)
        elif topic == info_response_topic(self._username):
            self._handle_info(payload)
        elif topic.endswith("/state") and "/ob/" in topic:
            self._handle_state(topic, payload)

    def _handle_details(self, payload: Any) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Could not parse Ampio devicesDetails")
            return
        for item in data.get("List", []):
            oid = _to_int(item.get("id"))
            if oid is None:
                continue
            typ = item.get("typ_komponentu")
            interp = _to_int(item.get("interpretacja"))
            obj = self.state.objects.get(oid) or AmpioObject(id=oid)
            obj.device_id = _to_int(item.get("id_urzadzenia"))
            obj.typ_komponentu = typ
            obj.name = item.get("opis_menu") or obj.name
            obj.interpretacja = interp
            obj.kind = classify_object(typ, interp)
            if obj.value is None:
                self._seed_from_stan_json(obj, item.get("stan_json"))
            self.state.objects[oid] = obj
            self._notify(obj)
        self._details_received.set()

    def _seed_from_stan_json(self, obj: AmpioObject, stan_json: Any) -> None:
        """Seed value from `stan_json` and bump the module's last_seen."""
        if not stan_json:
            return
        try:
            data = json.loads(stan_json)
        except (ValueError, TypeError):
            return
        obj.value = data.get("state", obj.value)
        self._touch_module(obj.device_id, data.get("on"))

    def _handle_devices(self, payload: Any) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Could not parse Ampio devices list")
            return
        for item in data.get("List", []):
            mid = _to_int(item.get("id"))
            if mid is None:
                continue
            typ = _to_int(item.get("typ_urzadzenia"))
            previous = self.state.modules.get(mid)
            self.state.modules[mid] = AmpioModule(
                id=mid,
                mac=_to_int(item.get("mac")),
                mac_global=_to_int(item.get("mac_global")),
                name=item.get("nazwa_urzadzenia") or None,
                type=typ,
                model=module_model(typ),
                sw_version=_to_int(item.get("wersja_softu")),
                hw_version=_to_int(item.get("wersja_pcb")),
                last_seen=previous.last_seen if previous else None,
            )
        self._devices_received.set()

    def _handle_info(self, payload: Any) -> None:
        """Parse the server info reply, keeping only safe fields."""
        self.state.server_info = _parse_server_info(payload)
        self._info_received.set()

    def _handle_states_snapshot(self, payload: Any) -> None:
        """Apply the bulk `data/states` snapshot.

        Seeds the value for every object whose entry has a non-empty
        `stan_json`. Existing fresh values from live pushes are preserved.
        """
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Could not parse Ampio states snapshot")
            return
        for item in data.get("List", []):
            oid = _to_int(item.get("id"))
            if oid is None:
                continue
            obj = self.state.objects.get(oid)
            if obj is None:
                # Metadata not yet known (e.g. snapshot arrived before details).
                obj = AmpioObject(id=oid, kind=classify_object(None, None))
                self.state.objects[oid] = obj
            if obj.value is None:
                self._seed_from_stan_json(obj, item.get("stan_json"))
            self._notify(obj)
        self._states_received.set()

    def _handle_state(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        # ampio / fromDB / <user> / ob / <id> / state
        if len(parts) < 6 or parts[3] != "ob":
            return
        oid = _to_int(parts[4])
        if oid is None:
            return
        value = payload
        on_ms: Any = None
        try:
            data = json.loads(payload)
            value = data.get("state", payload)
            on_ms = data.get("on")
        except (ValueError, TypeError):
            pass

        obj = self.state.objects.get(oid)
        if obj is None:
            # No metadata yet (e.g. restricted account) -> generic sensor.
            obj = AmpioObject(id=oid, kind=classify_object(None, None))
            self.state.objects[oid] = obj
        obj.value = value
        self._touch_module(obj.device_id, on_ms)
        self._notify(obj)

    def _touch_module(self, module_id: int | None, on_ms: Any) -> None:
        """Mark the module as having reported now (or at `on_ms`, server time)."""
        if module_id is None:
            return
        module = self.state.modules.get(module_id)
        if module is None:
            return
        if isinstance(on_ms, (int, float)) and on_ms > 0:
            ts = float(on_ms) / 1000.0
        else:
            ts = time.time()
        if module.last_seen is None or ts > module.last_seen:
            module.last_seen = ts

    def _notify(self, obj: AmpioObject) -> None:
        for listener in list(self._object_listeners):
            listener(obj)


def _parse_server_info(payload: Any) -> AmpioServerInfo:
    """Parse a server-info MQTT payload, keeping only the safe fields."""
    try:
        outer = json.loads(payload)
    except (ValueError, TypeError):
        _LOGGER.warning("Could not parse Ampio info payload")
        return AmpioServerInfo()
    data = outer.get("Results", outer) if isinstance(outer, dict) else {}
    if not isinstance(data, dict):
        return AmpioServerInfo()
    return AmpioServerInfo(
        mac=_to_int(data.get("mac")),
        server_version=data.get("serverVersion") or None,
        server_revision=data.get("serverRevision") or None,
        mqtt_version=data.get("mqttVersion") or None,
        local_ip=data.get("local_ip") or None,
        device_id=data.get("device_id") or None,
    )


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
