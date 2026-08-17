"""Async MQTT client for the Ampio DB-object protocol.

See ``docs/discovery-flow.md`` for the ``start()`` lifecycle and what
runs automatically vs on demand.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar, overload

from . import _connection, _protocol
from ._connection import _decode_payload
from ._store import AmpioStore
from .device_types import Capability
from .endpoints import (
    BASELINE_SERVER_VERSION,
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
from .errors import AmpioTimeoutError
from .events import (
    AuthFailed,
    AvailabilityChanged,
    ClientEvent,
    ConnectionDied,
)
from .models import (
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    ConnectionStats,
)

_LOGGER = logging.getLogger(__name__)

EventListener = Callable[[ClientEvent], None]
_EventT = TypeVar("_EventT", bound=ClientEvent)


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
            on_fatal=self._handle_fatal,
        )

        # One registry for every subscriber: (listener, event-type filter).
        self._listeners: list[
            tuple[Callable[[Any], None], tuple[type[ClientEvent], ...] | None]
        ] = []

        # Per-endpoint latch, set the first time each reply lands. Derived from
        # the endpoint table so a new endpoint needs no new field here.
        self._received: dict[str, asyncio.Event] = {
            ep.name: asyncio.Event() for ep in ENDPOINTS
        }
        # Last payload per endpoint as the broker sent it, so a consumer can
        # put the verbatim JSON into a diagnostics report without re-deriving
        # it. Append-only, and retained even when the payload fails to parse -
        # the bad bytes are exactly what a diagnostics report needs.
        self._last_payloads: dict[str, str] = {}
        # Callers of the on-demand fetches, awaiting the next parseable reply
        # per endpoint. Fetch correlation lives here, in futures; the
        # _received latches above answer only "has this endpoint ever
        # answered" for discovery.
        self._pending: dict[str, list[asyncio.Future[str]]] = {}

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
        """Apply one message, then dispatch what it changed."""
        self.stats.last_message_at = time.time()
        applied = self._store.apply(topic, payload)
        if applied.endpoint is not None:
            self._last_payloads[applied.endpoint.name] = payload
            # Latch and resolve waiters only on a payload that parsed, so a
            # malformed reply neither falsely completes discovery nor hands a
            # fetch garbage - the fetch keeps waiting for a good reply and
            # times out into the same retryable error as silence.
            if applied.parsed:
                self._received[applied.endpoint.name].set()
                for future in self._pending.pop(applied.endpoint.name, ()):
                    if not future.done():
                        future.set_result(payload)
        for event in applied.events:
            self._dispatch(event)

    def _handle_availability(self, available: bool) -> None:
        self._dispatch(AvailabilityChanged(available))

    def _handle_auth_failure(self, message: str) -> None:
        self._dispatch(AuthFailed(message))

    def _handle_fatal(self, message: str) -> None:
        self._dispatch(ConnectionDied(message))

    def _dispatch(self, event: ClientEvent) -> None:
        """Hand `event` to each matching listener, surviving any that raises.

        Listeners are consumer code. One that raises must not take down the
        connection that feeds every other entity, so the failure is logged
        and the remaining listeners still run.
        """
        for listener, only in list(self._listeners):
            if only is not None and not isinstance(event, only):
                continue
            try:
                listener(event)
            except Exception:
                _LOGGER.exception("Ampio event listener raised")

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
        a collision appears; one that resolves clears the set silently.
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
        info reply arrives, or when it carries no account id (a
        below-baseline server, warned at discovery). Settled by the time
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
        ``params_devices``, ``groups``, ``group_devices``, ``locations``,
        ``scenes``); an endpoint absent until its first reply lands.
        """
        return self._last_payloads

    @overload
    def subscribe(self, listener: EventListener) -> Callable[[], None]: ...

    @overload
    def subscribe(
        self, listener: Callable[[_EventT], None], *, of: type[_EventT]
    ) -> Callable[[], None]: ...

    @overload
    def subscribe(
        self, listener: EventListener, *, of: tuple[type[ClientEvent], ...]
    ) -> Callable[[], None]: ...

    def subscribe(
        self,
        listener: Callable[[Any], None],
        *,
        of: type | tuple[type, ...] | None = None,
    ) -> Callable[[], None]:
        """Register ``listener`` on the event stream; returns an unsubscribe.

        Everything the client learns flows through this one stream, in the
        order it was produced: ``ObjectUpdated``, ``ObjectRemoved``,
        ``ModuleUpdated``, ``ModuleRemoved``, ``BusEvent``,
        ``AvailabilityChanged``, ``AuthFailed``, and ``ConnectionDied`` -
        see :mod:`ampio_mqtt.events` for what each means and which account
        tiers produce it. ``of`` narrows the subscription to one event class
        (which also types the callback parameter precisely) or a tuple of
        classes::

            client.subscribe(on_any_event)
            client.subscribe(on_object, of=ObjectUpdated)
            client.subscribe(on_gone, of=(ObjectRemoved, ModuleRemoved))

        Ordering across kinds is guaranteed: the availability drop precedes
        the terminal ``AuthFailed`` / ``ConnectionDied``, and removals
        follow the updates of the catalogue reply that caused them.
        """
        only = (of,) if isinstance(of, type) else of
        entry = (listener, only)
        self._listeners.append(entry)

        def _unsubscribe() -> None:
            self._listeners.remove(entry)

        return _unsubscribe

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
        parsed = _protocol.parse_server_info(payload)
        if _protocol.server_below_baseline(parsed.server_version):
            _LOGGER.warning(
                "Ampio server reports version %s, below the tested baseline %s; "
                "behavior on this server is untested - upgrade the M-SERV",
                parsed.server_version or "(none)",
                ".".join(map(str, BASELINE_SERVER_VERSION)),
            )
        return parsed

    async def start(
        self, *, timeout: float = 15.0, discovery_timeout: float = 8.0
    ) -> bool:
        """Start the connection, wait for connect and initial discovery.

        After connecting, waits up to `discovery_timeout` for the initial
        object catalogue so names and classification are known before entities
        are created. Admin accounts complete via the `config` surface (objects
        plus the module list); non-admin accounts complete via the app-sync
        `data` surface (grant-filtered objects, no modules). See `access_tier`
        for the detected tier.

        Returns True when that discovery cycle completed in time and False
        when `discovery_timeout` elapsed first. A False leaves the connection
        up and discovery continuing opportunistically, so the caller can
        await :meth:`wait_for_initial_discovery` (the explicit form of the
        same guarantee) rather than restarting. A consumer that must read
        `modules`/`objects`/`server_info` before building on top of the
        client should check this result or call that method rather than
        relying on `start()`'s timing.
        """
        await self._connection.open(timeout)
        return await self.wait_for_initial_discovery(timeout=discovery_timeout)

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
        payloads = await self._fetch(
            ("groups", "group_devices"),
            timeout,
            "Timed out fetching room map from Ampio broker",
        )
        return _protocol.parse_rooms(payloads["groups"], payloads["group_devices"])

    async def fetch_scenes(self, timeout: float = 5.0) -> list[AmpioScene]:
        """Return the scene catalogue defined in the Ampio app.

        Requires ``start()`` to have completed. Raises ``AmpioConnectionError``
        if the broker is not connected and ``AmpioTimeoutError`` if the
        response does not arrive within ``timeout``.
        """
        payloads = await self._fetch(
            ("scenes",), timeout, "Timed out fetching scenes from Ampio broker"
        )
        return _protocol.parse_scenes(payloads["scenes"]) or []

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
        payloads = await self._fetch(
            ("locations",),
            timeout,
            "Timed out fetching locations table from Ampio broker",
        )
        return _protocol.parse_locations(payloads["locations"])

    # --- commands ---------------------------------------------------------

    async def command(self, object_id: int, verb: str, *args: object) -> None:
        """Send ``verb`` (with any args) to an object on the command surface.

        Publishes ``/api/set/<object_id>/<verb>[/<arg>...]`` at QoS 1 and
        returns once the broker acknowledges it - "the broker accepted the
        command", not "the M-SERV applied it". The M-SERV applies it
        asynchronously and the resulting state arrives through the normal
        object listeners (typically within a few hundred ms). Nothing is echoed
        back for an unknown verb or an object that cannot perform it - the
        M-SERV simply ignores it.

        This is the escape hatch for the verbs the library does not wrap: the
        vocabulary is the M-SERV's own, so anything its HTTP API accepts works
        here (``setTemperature``, ``arm``, ``setVolume``, ``setText``, ...).
        See docs/protocol.md.

        Commands are grant-scoped exactly as reads are: on a standard
        account a command for an object outside the grant is dropped with
        no effect and no reply, while an administrator commands any object.

        Raises ``AmpioConnectionError`` when the broker is unreachable and
        ``AmpioTimeoutError`` when it fails to acknowledge in time; never an
        aiomqtt exception type.
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

    async def _fetch(
        self, names: tuple[str, ...], timeout: float, timeout_message: str
    ) -> dict[str, str]:
        """Request the given endpoints and return each reply payload by name.

        One future per endpoint correlates this caller with the next parseable
        reply, so concurrent fetches never disturb each other and the
        discovery latches stay untouched; every concurrent caller of the same
        endpoint receives the same reply. The wire carries no correlation
        ids - a reply already in flight from an earlier request can satisfy a
        later ask, which for these idempotent read endpoints is the intended
        semantics. A reply that does not parse resolves nothing (see
        ``_handle_message``), so a corrupt reply ends in the same retryable
        ``AmpioTimeoutError`` as no reply at all.
        """
        loop = asyncio.get_running_loop()
        futures: dict[str, asyncio.Future[str]] = {
            name: loop.create_future() for name in names
        }
        for name, future in futures.items():
            self._pending.setdefault(name, []).append(future)
        try:
            for name in names:
                await self._publish(ENDPOINT_BY_NAME[name])
            async with asyncio.timeout(timeout):
                await asyncio.gather(*futures.values())
        except TimeoutError as err:
            raise AmpioTimeoutError(timeout_message) from err
        finally:
            # A resolved future was already dropped by the dispatcher; on
            # timeout, cancellation, or a failed publish, remove this call's
            # own waiters so a late reply resolves nothing stale.
            for name, future in futures.items():
                waiters = self._pending.get(name)
                if waiters is not None and future in waiters:
                    waiters.remove(future)
                    if not waiters:
                        del self._pending[name]
        return {name: future.result() for name, future in futures.items()}

    def _feed_message(self, topic: str, payload: str | bytes) -> None:
        """Inject a message directly into the routing logic.

        Private entry point used by the library's own tests; the real broker
        drives the same path through the connection.
        """
        self._handle_message(topic, _decode_payload(payload))


def _check_range(name: str, value: int, low: int, high: int) -> None:
    """Reject a mis-typed or out-of-range command argument before the wire.

    Rejects bool explicitly: it passes ``isinstance(int)`` (and the type
    checker, which treats bool as an int subtype), but the wire encoding is
    ``str()``, so a bool would go out as the literal ``True`` - a malformed
    command the M-SERV silently drops.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an int in {low}..{high}, got {value!r}")
