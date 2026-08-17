"""Async MQTT client for the Ampio DB-object protocol.

See ``docs/discovery-flow.md`` for the ``start()`` lifecycle and what
runs automatically vs on demand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from . import _connection, _protocol
from ._store import AmpioStore
from .const import (
    DISCOVERY_ADMIN,
    DISCOVERY_COMMON,
    DISCOVERY_FALLBACK,
    ENDPOINT_BY_NAME,
    ENDPOINTS,
    KEEP_POSITION,
    RAW_DIAGNOSTICS_WILDCARD,
    RAW_EVENT_WILDCARD,
    RAW_INPUT_WILDCARDS,
    AccessTier,
    Endpoint,
    command_payload,
    command_topic,
    event_payload,
    ob_state_wildcard,
    request_topic,
    response_topic,
    scene_payload,
)
from .device_types import Capability
from .errors import AmpioTimeoutError
from .models import (
    AmpioEvent,
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    ConnectionStats,
)
from .rooms import join_rooms

_LOGGER = logging.getLogger(__name__)

ObjectListener = Callable[[AmpioObject], None]
ModuleListener = Callable[[AmpioModule], None]
EventListener = Callable[[AmpioEvent], None]
AvailabilityListener = Callable[[bool], None]
AuthFailureListener = Callable[[str], None]


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
        self._username = username or ""
        self._store = AmpioStore(self._username)
        self.stats = ConnectionStats()
        self._connection = _connection.Connection(
            host,
            port,
            username,
            password,
            reconnect_interval=reconnect_interval,
            topics=self._subscriptions(),
            stats=self.stats,
            on_message=self._handle_message,
            on_availability=self._handle_availability,
            on_connected=self.refresh,
            on_auth_failure=self._handle_auth_failure,
        )

        self._object_listeners: list[ObjectListener] = []
        self._module_listeners: list[ModuleListener] = []
        self._event_listeners: list[EventListener] = []
        self._availability_listeners: list[AvailabilityListener] = []
        self._auth_failure_listeners: list[AuthFailureListener] = []

        # Per-endpoint latch, set the first time each reply lands. Derived from
        # the endpoint table so a new endpoint needs no new field here.
        self._received: dict[str, asyncio.Event] = {
            ep.name: asyncio.Event() for ep in ENDPOINTS
        }
        # Last payload per endpoint as the broker sent it: the on-demand
        # fetches parse theirs out of here, and a consumer can put the verbatim
        # JSON into a diagnostics report without re-deriving it.
        self._last_payloads: dict[str, str] = {}

    def _subscriptions(self) -> list[str]:
        """Every topic the client needs on each (re)connect."""
        return [
            *(response_topic(ep, self._username) for ep in ENDPOINTS),
            ob_state_wildcard(self._username),
            *RAW_INPUT_WILDCARDS,
            RAW_DIAGNOSTICS_WILDCARD,
            RAW_EVENT_WILDCARD,
        ]

    def _handle_message(self, topic: str, payload: str) -> None:
        """Apply one message, then tell whoever the change concerns."""
        self.stats.last_message_at = time.time()
        applied = self._store.apply(topic, payload)
        if applied.endpoint is not None:
            self._last_payloads[applied.endpoint.name] = payload
            # Latch only on a payload that parsed, so a malformed reply does
            # not falsely complete discovery.
            if applied.parsed:
                self._received[applied.endpoint.name].set()
        for obj in applied.objects:
            _emit(self._object_listeners, obj, "object")
        for module in applied.modules:
            _emit(self._module_listeners, module, "module")
        for event in applied.events:
            _emit(self._event_listeners, event, "event")

    def _handle_availability(self, available: bool) -> None:
        _emit(self._availability_listeners, available, "availability")

    def _handle_auth_failure(self, message: str) -> None:
        _emit(self._auth_failure_listeners, message, "auth failure")

    # --- public API -------------------------------------------------------

    @property
    def objects(self) -> dict[int, AmpioObject]:
        """All known objects keyed by id."""
        return self._store.objects

    @property
    def modules(self) -> dict[int, AmpioModule]:
        """All known physical modules keyed by id."""
        return self._store.modules

    @property
    def server_info(self) -> AmpioServerInfo | None:
        """The Ampio M-SERV self-reported info, if discovered."""
        return self._store.server_info

    @property
    def colliding_macs(self) -> frozenset[int]:
        """Effective bus macs the devices catalogue reports on 2+ modules.

        Empty on a correctly commissioned install; nothing on the wire
        enforces uniqueness, so a misconfigured or mid-commissioning install
        can collide. A colliding mac makes `AmpioModule.mac` - the
        recommended replacement-stable module key - ambiguous: a consumer
        keying devices on it should skip or disambiguate these modules
        instead of silently merging them. While a mac collides the library
        routes no raw-channel input events or diagnostics broadcasts for it
        (the sender is unknowable); affected inputs still update through the
        per-object state path. A warning naming the modules is logged when
        the set changes.
        """
        return self._store.colliding_macs

    @property
    def mserv_id(self) -> int | None:
        """Resolve the module id of the M-SERV server.

        Prefers cross-validating the server's self-reported mac against each
        module's mac_global/mac; falls back to the unique module the device
        catalogue marks as a hub.
        """
        info = self._store.server_info
        if info is not None and info.mac is not None:
            for mid, mod in self._store.modules.items():
                if info.mac in (mod.mac_global, mod.mac):
                    return mid
        candidates = [
            mid
            for mid, mod in self._store.modules.items()
            if Capability.HUB in mod.capabilities
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    @property
    def sensors(self) -> dict[int, AmpioObject]:
        """Objects classified as sensors."""
        return self._store.state.sensors

    @property
    def available(self) -> bool:
        """Whether the broker connection is up."""
        return self._connection.available

    @property
    def auth_failure(self) -> str | None:
        """Why the connection stopped, if the broker rejected the credentials.

        ``None`` while the credentials are accepted, including through outages
        the client is still trying to reconnect across. Once set, the
        connection loop has stopped for good and only a fresh ``start()`` -
        presumably with new credentials - clears it.
        """
        return self._connection.auth_failure

    @property
    def access_tier(self) -> AccessTier:
        """Detected account tier, read from the server-info reply.

        The info surface answers for every tier and reports the account's
        own id: ``-1`` for the reserved ``admin`` login, the users-table row
        id for an app-created (always non-admin) user. ``UNKNOWN`` until the
        info reply arrives, or when it carries no account id (firmware
        predating the field). Settled by the time
        :meth:`wait_for_initial_discovery` returns True; a config flow can
        read the same answer from :meth:`test_connection`'s result before
        any client exists. Per-user app permissions do not move an account
        between tiers.
        """
        info = self._store.server_info
        return info.access_tier if info is not None else AccessTier.UNKNOWN

    @property
    def last_payloads(self) -> dict[str, str]:
        """Verbatim last response payload per endpoint, keyed by endpoint name.

        Retained for the HA integration's diagnostics blob so a report can
        include the actual JSON the M-SERV emitted. Keys are endpoint names
        (``details``, ``devices``, ``states``, ``info``, ``data_devices``,
        ``params_devices``, ``groups``, ``group_devices``, ``locations``); an
        endpoint absent until its first reply lands.
        """
        return self._last_payloads

    def add_object_listener(self, listener: ObjectListener) -> Callable[[], None]:
        """Register a callback invoked on every object update (state/metadata)."""
        self._object_listeners.append(listener)
        return lambda: self._object_listeners.remove(listener)

    def add_module_listener(self, listener: ModuleListener) -> Callable[[], None]:
        """Register a callback invoked when a module's own report updates it.

        Fires on the diagnostics broadcast, so it is administrator-only; a
        standard account never receives the raw tree and this never fires.
        """
        self._module_listeners.append(listener)
        return lambda: self._module_listeners.remove(listener)

    def add_event_listener(self, listener: EventListener) -> Callable[[], None]:
        """Register a callback invoked when a bus event is raised.

        Received events ride the administrator-only raw tree, so this never
        fires on a standard account even though such an account can raise
        events itself.
        """
        self._event_listeners.append(listener)
        return lambda: self._event_listeners.remove(listener)

    def add_availability_listener(
        self, listener: AvailabilityListener
    ) -> Callable[[], None]:
        """Register a callback invoked when connection availability changes.

        Fires for every transition the consumer did not cause itself: the
        connection coming up, an outage, and the fatal auth-failure stop
        (before its own listener, so entities read unavailable by then). A
        consumer-initiated ``stop()`` is deliberately not reported - it is
        not news to the consumer, and reporting it made every orderly
        shutdown look like a lost connection. ``available`` still reads
        False after a stop.
        """
        self._availability_listeners.append(listener)
        return lambda: self._availability_listeners.remove(listener)

    def add_auth_failure_listener(
        self, listener: AuthFailureListener
    ) -> Callable[[], None]:
        """Register a callback for a fatal credential rejection after start.

        Invoked with the broker's reason string when a reconnect attempt is
        rejected as unauthorized after ``start()`` has succeeded - the shape a
        credential change on the broker produces. By the time it fires the
        availability listeners have reported False and the connection loop has
        stopped for good, so this is the signal to drive a reauthentication
        flow; without it a dead loop is indistinguishable from an outage the
        client is still retrying. A rejection during ``start()`` itself raises
        ``AmpioAuthError`` there instead and does not invoke this listener.
        The reason is also queryable as :pyattr:`auth_failure`.
        """
        self._auth_failure_listeners.append(listener)
        return lambda: self._auth_failure_listeners.remove(listener)

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

        Raises ``AmpioAuthError`` on credential rejection, ``AmpioTimeoutError``
        when the connection succeeds but no info reply arrives within
        ``info_timeout`` (slow or overloaded broker - worth retrying), and
        ``AmpioConnectionError`` on any other connection failure. The info
        surface answers with full identity for every account tier, so a reply
        that arrives without identity fields is returned as-is rather than
        raised: it means the server is answering but has nothing to say, not
        that the request was too slow.

        The result's ``access_tier`` tells a config flow what the account
        will get before any client exists: a ``RESTRICTED`` account never
        receives the module list, so a consumer that needs ``modules`` or
        ``mserv_id`` can reject it here with an accurate message instead of
        failing at setup.
        """
        user = username or ""
        info = ENDPOINT_BY_NAME["info"]
        payload = await _connection.probe(
            host,
            port,
            username,
            password,
            request_topic=request_topic(info, user),
            request_payload=info.req_payload,
            reply_topic=response_topic(info, user),
            timeout=info_timeout,
        )
        if payload is None:
            raise AmpioTimeoutError(
                f"No server-info reply from the Ampio broker within {info_timeout}s"
            )
        return _protocol.parse_server_info(payload)

    async def start(
        self, *, timeout: float = 15.0, discovery_timeout: float = 8.0
    ) -> None:
        """Start the connection, wait for connect and initial discovery.

        After connecting, waits up to `discovery_timeout` for the initial
        object catalogue so names and classification are known before entities
        are created. Admin accounts complete via the `config` surface (objects
        plus the module list); non-admin accounts complete via the app-sync
        `data` surface (grant-filtered objects, no modules). See `access_tier`
        for the detected tier.

        On return, the initial discovery cycle has been awaited up to
        `discovery_timeout`; see `wait_for_initial_discovery` for the explicit,
        opt-in form of that guarantee. A consumer that must read
        `modules`/`objects`/`server_info` before building on top of the client
        should call that method rather than relying on `start()`'s timing.
        """
        await self._connection.open(timeout)
        await self.wait_for_initial_discovery(timeout=discovery_timeout)

    async def wait_for_initial_discovery(self, *, timeout: float = 8.0) -> bool:
        """Block until the initial discovery cycle has populated the client.

        Waits for the states snapshot and info replies, reads the account
        tier off the info reply, then waits for that tier's object catalogue
        pair: the admin ``config`` pair (devicesDetails -> ``objects``,
        devices -> ``modules``) or the ``data`` pair (data/devices ->
        grant-filtered ``objects``, data/params_devices -> visibility flags).
        An ``UNKNOWN`` tier waits on the ``data`` pair, which answers for
        every account. Returns True on completion and False if ``timeout``
        elapses first.

        This is the contract a consumer relies on when it must read
        ``objects``/``server_info`` (and, on the admin tier, ``modules``)
        before building anything on top of the client. It never raises on
        timeout - discovery continues opportunistically and this simply
        returns False.

        Safe to call repeatedly and after reconnects - the underlying signals
        latch on first completion, so once discovery has happened this returns
        immediately.
        """

        async def _all(names: tuple[str, ...]) -> None:
            await asyncio.gather(*(self._received[n].wait() for n in names))

        try:
            async with asyncio.timeout(timeout):
                await _all(DISCOVERY_COMMON)
                admin = self.access_tier is AccessTier.ADMIN
                await _all(DISCOVERY_ADMIN if admin else DISCOVERY_FALLBACK)
        except TimeoutError:
            return False
        return True

    async def stop(self) -> None:
        """Stop the connection.

        Safe to call at any point, including when the connection loop has
        already failed: whatever it died of is logged rather than raised, so a
        consumer can always tear the client down. The availability listeners
        are not invoked for the resulting drop - a deliberate shutdown is not
        an availability event.
        """
        await self._connection.close()

    async def request(self, name: str) -> None:
        """Publish the request keyword for endpoint ``name``.

        The reply lands asynchronously on that endpoint's response topic and is
        applied by the dispatcher. ``name`` is one of the keys in the endpoint
        table (``details``, ``devices``, ``states``, ``info``, ...). Use
        :meth:`fetch_rooms` / :meth:`fetch_locations` for the on-demand
        endpoints whose reply you need to read back synchronously.
        """
        await self._publish(ENDPOINT_BY_NAME[name])

    async def refresh(self) -> None:
        """Re-request the full initial-discovery set (both object catalogues,
        modules, params, states, info).

        ``start()`` issues this once on every (re)connect; call it to force a
        fresh discovery cycle without reconnecting.
        """
        for ep in ENDPOINTS:
            if ep.initial:
                await self._publish(ep)

    async def fetch_rooms(self, timeout: float = 5.0) -> dict[int, str]:
        """Return ``{ampio_object_id: room_name}`` for objects assigned to a room.

        Publishes the ``groups`` and ``group_devices`` keywords to
        ``ampio/control/<user>/data`` and awaits both responses on
        ``ampio/fromDB/<user>/data/<keyword>``. Joins them in memory; objects
        assigned to multiple groups map to the first room encountered (Home
        Assistant allows one area per device).

        Requires ``start()`` to have completed. Raises ``AmpioConnectionError``
        if the broker is not connected and ``AmpioTimeoutError`` if either
        response does not arrive within ``timeout``.
        """
        await self._request_and_wait(
            ("groups", "group_devices"),
            timeout,
            "Timed out fetching room map from Ampio broker",
        )
        return join_rooms(
            _safe_json_object(self._last_payloads.get("groups")),
            _safe_json_object(self._last_payloads.get("group_devices")),
        )

    async def fetch_scenes(self, timeout: float = 5.0) -> list[AmpioScene]:
        """Return the scene catalogue defined in the Ampio app.

        Requires ``start()`` to have completed. Raises ``AmpioConnectionError``
        if the broker is not connected and ``AmpioTimeoutError`` if the
        response does not arrive within ``timeout``.
        """
        await self._request_and_wait(
            ("scenes",), timeout, "Timed out fetching scenes from Ampio broker"
        )
        return _protocol.parse_scenes(self._last_payloads.get("scenes") or "") or []

    async def send_event(self, event_number: int) -> None:
        """Raise a bus event, running whatever Ampio logic is bound to it.

        Works on both account tiers and is bounded by neither object grants
        nor the per-event rights the Ampio app shows: a standard account
        raises any event number it likes. Since the logic behind an event can
        drive anything, this is the one way an account reaches objects it
        cannot command directly.
        """
        _check_range("event_number", event_number, 1, 65535)
        await self._connection.publish(
            command_topic(self._username), event_payload(event_number).encode()
        )

    async def run_scene(self, scene_id: int) -> None:
        """Apply a scene's actions."""
        await self._scene_command(scene_id, "run")

    async def turn_scene_off(self, scene_id: int) -> None:
        """Turn off the objects a scene drives."""
        await self._scene_command(scene_id, "off")

    async def undo_scene(self, scene_id: int) -> None:
        """Restore the objects a scene drives to the state they held before it ran."""
        await self._scene_command(scene_id, "undo")

    async def _scene_command(self, scene_id: int, verb: str) -> None:
        """Publish a scene command; the M-SERV replays the scene's own actions.

        Like any other command these are bounded by the account's grant, so a
        scene touching objects outside it does nothing.
        """
        await self._connection.publish(
            command_topic(self._username), scene_payload(scene_id, verb).encode()
        )

    async def fetch_locations(self, timeout: float = 5.0) -> dict[int, str]:
        """Return ``{location_id: name}`` for the Designer "Location" markers.

        The location is the user-editable per-output marker visible in the
        Designer's "Lokalizacja" column (e.g. ``Salon``, ``Kuchnia``, ...).
        It is **per-output**, not per-module: each module's outputs can be
        assigned to different locations, and the per-output assignment lives
        in the device's CAN-resident description table (not exposed via
        MQTT - see docs/untapped-surfaces.md for the RPC route that would
        resolve it).

        This method returns only the *name table* - the integer ID -> human
        label mapping the Designer uses to populate its dropdown. A consumer
        that does have a way to learn the per-output integer can resolve it
        through this dict. Without that, the table is still useful in
        diagnostics ("which location ids does this M-SERV define?").

        Publishes ``locations`` to ``ampio/control/<user>/config`` and awaits
        the response on ``ampio/fromDB/<user>/config/locations``. Requires
        ``start()`` to have completed. Raises ``AmpioConnectionError`` if the
        broker is not connected and ``AmpioTimeoutError`` if the response does
        not arrive within ``timeout``.
        """
        await self._request_and_wait(
            ("locations",),
            timeout,
            "Timed out fetching locations table from Ampio broker",
        )
        data = _safe_json_object(self._last_payloads.get("locations"))
        out: dict[int, str] = {}
        for item in data.get("List", []):
            if not isinstance(item, dict):
                continue
            lid = item.get("id")
            name = item.get("opis_menu")
            if isinstance(lid, int) and isinstance(name, str) and name:
                out[lid] = name
        return out

    # --- commands ---------------------------------------------------------

    async def command(self, object_id: int, verb: str, *args: object) -> None:
        """Send ``verb`` (with any args) to an object on the command surface.

        Publishes ``/api/set/<object_id>/<verb>[/<arg>...]`` and returns as soon
        as the publish completes; the M-SERV applies it asynchronously and the
        resulting state arrives through the normal object listeners (typically
        within a few hundred ms). Nothing is echoed back for an unknown verb or
        an object that cannot perform it - the M-SERV simply ignores it.

        This is the escape hatch for the verbs the library does not wrap: the
        vocabulary is the M-SERV's own, so anything its HTTP API accepts works
        here (``setTemperature``, ``arm``, ``setVolume``, ``setText``, ...).
        See docs/protocol.md.

        Note the M-SERV does not restrict commands to the objects an account
        was granted in the app; a non-admin account can command any object.
        """
        await self._connection.publish(
            command_topic(self._username),
            command_payload(object_id, verb, args).encode(),
        )

    async def turn_on(self, object_id: int) -> None:
        """Turn an object fully on."""
        await self.command(object_id, "turnOn")

    async def turn_off(self, object_id: int) -> None:
        """Turn an object off."""
        await self.command(object_id, "turnOff")

    async def toggle(self, object_id: int) -> None:
        """Invert an object's current on/off state."""
        await self.command(object_id, "switch")

    async def set_value(
        self, object_id: int, value: int, *, pulse_ms: int | None = None
    ) -> None:
        """Set an object's 0-255 level (relay, flag, dimmer).

        With ``pulse_ms`` the M-SERV reverts the object to its previous state
        after that many milliseconds - a timed pulse, not a fade. The wire unit
        is 10 ms, so the value is rounded down to the nearest 10 ms; a gate
        pulse of 500 ms is ``pulse_ms=500``.
        """
        _check_range("value", value, 0, 255)
        if pulse_ms is None:
            await self.command(object_id, "setValue", value)
            return
        _check_range("pulse_ms", pulse_ms, 0, 655350)
        await self.command(object_id, "setValue", value, pulse_ms // 10)

    async def set_color(
        self, object_id: int, red: int, green: int, blue: int, white: int = 0
    ) -> None:
        """Set an RGBW object's four channels, each 0-255."""
        for name, channel in (
            ("red", red),
            ("green", green),
            ("blue", blue),
            ("white", white),
        ):
            _check_range(name, channel, 0, 255)
        await self.command(object_id, "setColors", red, green, blue, white)

    async def open_cover(self, object_id: int) -> None:
        """Drive a cover to fully open (position 100)."""
        await self.command(object_id, "open")

    async def close_cover(self, object_id: int) -> None:
        """Drive a cover to fully closed (position 0)."""
        await self.command(object_id, "close")

    async def set_cover_position(
        self, object_id: int, position: int, *, lamella: int | None = None
    ) -> None:
        """Drive a cover to ``position`` percent (0 closed, 100 open).

        ``lamella`` sets the slat angle of a blind that has one, in the same
        command. Omitting it sends no angle, which is not the same as holding
        one: travel drags the slats along mechanically and leaves them closed
        after a downward move and open after an upward one, so pass
        ``lamella`` to land on a chosen angle. Position updates stream in as
        the cover travels, so a consumer sees the movement rather than one
        jump to the target.
        """
        _check_range("position", position, 0, 100)
        if lamella is not None:
            _check_range("lamella", lamella, 0, 100)
        await self.command(
            object_id,
            "setRollerPos",
            position,
            KEEP_POSITION if lamella is None else lamella,
        )

    async def set_cover_tilt(self, object_id: int, lamella: int) -> None:
        """Set a blind's slat angle percent, leaving its position alone."""
        _check_range("lamella", lamella, 0, 100)
        await self.command(object_id, "setRollerPos", KEEP_POSITION, lamella)

    async def _publish(self, ep: Endpoint) -> None:
        """Publish an endpoint's request keyword to its control topic."""
        await self._connection.publish(
            request_topic(ep, self._username), ep.req_payload.encode()
        )

    async def _request_and_wait(
        self, names: tuple[str, ...], timeout: float, timeout_message: str
    ) -> None:
        """Re-request the given endpoints and block until each reply latches.

        Clears each endpoint's latch and retained payload first so a stale prior
        reply can't satisfy the wait, then publishes and awaits all of them.
        """
        for name in names:
            self._received[name].clear()
            self._last_payloads.pop(name, None)
        for name in names:
            await self._publish(ENDPOINT_BY_NAME[name])
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*(self._received[n].wait() for n in names))
        except TimeoutError as err:
            raise AmpioTimeoutError(timeout_message) from err

    def _feed_message(self, topic: str, payload: str | bytes) -> None:
        """Inject a message directly into the routing logic.

        Private entry point used by the library's own tests; the real broker
        drives the same path through the connection.
        """
        self._handle_message(topic, _decode_payload(payload))

    # --- internal ---------------------------------------------------------


def _emit(listeners: list[Any], payload: Any, kind: str) -> None:
    """Hand `payload` to each listener, surviving any that raises.

    Listeners are consumer code. One that raises must not take down the
    connection that feeds every other entity, so the failure is logged and the
    remaining listeners still run.
    """
    for listener in list(listeners):
        try:
            listener(payload)
        except Exception:
            _LOGGER.exception("Ampio %s listener raised", kind)


def _check_range(name: str, value: int, low: int, high: int) -> None:
    """Reject an out-of-range command argument before it reaches the wire."""
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an int in {low}..{high}, got {value!r}")


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
