"""Async MQTT client for the Ampio DB-object protocol.

See ``docs/discovery-flow.md`` for the ``start()`` lifecycle and what
runs automatically vs on demand.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, TypeVar, cast, overload

from . import _connection, _protocol
from ._protocol import (
    ADMIN_USERNAME,
    ENDPOINT_BY_NAME,
    ENDPOINTS,
    KEEP_POSITION,
    RAW_DIAGNOSTICS_WILDCARD,
    RAW_EVENT_WILDCARD,
    RAW_INPUT_WILDCARDS,
    Endpoint,
    command_payload,
    command_topic,
    event_payload,
    ob_state_wildcard,
    request_topic,
    response_topic,
    scene_payload,
)
from ._store import AmpioStore
from .classification import OutputKind
from .device_types import is_hub
from .errors import AmpioTimeoutError
from .events import (
    AuthFailed,
    AvailabilityChanged,
    ClientEvent,
    ConnectionDied,
    ObjectUpdated,
)
from .models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    ConnectionStats,
)

_LOGGER = logging.getLogger(__name__)

EventListener = Callable[[ClientEvent], None]
_EventT = TypeVar("_EventT", bound=ClientEvent)


class _ReplyChannel:
    """Everything the client tracks about one endpoint's replies.

    One instance per endpoint-table row, so a new endpoint needs no new
    client plumbing: ``received`` latches on the first parsed reply (the
    discovery signal - it never clears, which is what makes
    ``wait_for_initial_discovery`` instant after a reconnect),
    ``last_payload`` keeps the verbatim payload for the diagnostics blob,
    and ``waiters`` are fetch futures awaiting the next parsed reply.
    """

    __slots__ = ("last_payload", "received", "waiters")

    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.last_payload: str | None = None
        self.waiters: list[asyncio.Future[Any]] = []

    def deliver(self, payload: str, parsed: object | None) -> None:
        """Record one reply; ``parsed`` is its parsed form, None when the
        payload could not be read.

        A malformed reply must neither falsely complete discovery nor hand a
        fetch garbage - the fetch keeps waiting and times out into the same
        retryable error as silence. The bad bytes still land in
        ``last_payload``: they are exactly what a diagnostics report needs.
        """
        self.last_payload = payload
        if parsed is None:
            return
        self.received.set()
        waiters, self.waiters = self.waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(parsed)


class AmpioClient:
    """Maintains a connection to the Ampio broker and tracks object state."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str | None = None,
        *,
        port: int = 1883,
        reconnect_interval: float = 5.0,
        mqtt_client_factory: _connection.MqttClientFactory | None = None,
    ) -> None:
        """Initialize the client. `username` names the Ampio account and
        namespaces every MQTT topic; an empty one is rejected here because
        the client would otherwise subscribe to a namespace no M-SERV
        serves and fail only minutes later as "discovery never completes".

        ``mqtt_client_factory`` is the transport seam: a zero-argument
        callable returning the MQTT session object for one connect attempt.
        Leave it None for the real broker connection; a test injects a fake
        broker instance here instead of patching aiomqtt.
        """
        if not username:
            raise ValueError(
                "username is required - the Ampio topics are namespaced by account"
            )
        self._username = username
        # The tier is the authenticated login name - see AccessTier. It
        # shapes everything downstream: the endpoints served, and from them
        # the subscriptions, the router, the reply channels, and the
        # initial-discovery set.
        self._tier = (
            AccessTier.ADMIN if username == ADMIN_USERNAME else AccessTier.RESTRICTED
        )
        self._served = tuple(ep for ep in ENDPOINTS if ep.tier in (None, self._tier))
        self._initial_endpoints = tuple(ep.name for ep in self._served if ep.initial)
        self._router = _protocol.Router(username, self._served)
        self._store = AmpioStore()
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
            client_factory=mqtt_client_factory,
        )

        # One registry for every subscriber: (listener, event-type filter).
        self._listeners: list[
            tuple[Callable[[Any], None], tuple[type[ClientEvent], ...] | None]
        ] = []

        # One reply channel per endpoint-table row the tier is served -
        # see _ReplyChannel. The router covers the same set, so every
        # routed reply has a channel.
        self._channels: dict[str, _ReplyChannel] = {
            ep.name: _ReplyChannel() for ep in self._served
        }

        # Topics whose messages have failed processing, so a recurring
        # poison payload logs its traceback once instead of per delivery.
        self._poisoned_topics: set[str] = set()

    def _subscriptions(self) -> list[str]:
        """Every topic the client needs on each (re)connect."""
        topics = [
            *(response_topic(ep, self._username) for ep in self._served),
            ob_state_wildcard(self._username),
        ]
        if self._tier is AccessTier.ADMIN:
            # The raw tree is served to the admin login alone; any other
            # client never asks, so a SUBACK rejection is always a fault.
            topics += [
                *RAW_INPUT_WILDCARDS,
                RAW_DIAGNOSTICS_WILDCARD,
                RAW_EVENT_WILDCARD,
            ]
        return topics

    def _handle_message(self, topic: str, payload: str) -> None:
        """Apply one message, then dispatch what it changed.

        Guarded per message: a processing bug costs the one message that
        triggered it, never the connection - unguarded, the exception would
        reach the connection loop's terminal path, and a retained poison
        payload would then kill the client seconds after every restart.
        The traceback is logged once per topic (repeats at debug, the
        anti-drowning convention); bugs in the connection loop itself
        remain terminal.
        """
        self.stats.last_message_at = time.time()
        try:
            msg = self._router.route(topic, payload)
            if msg is None:
                return
            if (
                isinstance(msg, _protocol.EndpointReply)
                and msg.endpoint.parses is not None
            ):
                # Pure request/response: the endpoint's own parser is the
                # gate, run exactly once - its output is what a fetch
                # returns. Nothing here mutates the store.
                parsed = msg.endpoint.parses(payload)
                if parsed is None:
                    _LOGGER.warning("Could not parse Ampio %s reply", msg.endpoint.name)
                self._channels[msg.endpoint.name].deliver(payload, parsed)
                return
            applied = self._store.apply(msg)
            if isinstance(msg, _protocol.EndpointReply):
                self._channels[msg.endpoint.name].deliver(
                    payload, applied.parsed or None
                )
        except Exception:
            if topic in self._poisoned_topics:
                _LOGGER.debug("Dropped another failing Ampio message on %s", topic)
            else:
                self._poisoned_topics.add(topic)
                _LOGGER.exception(
                    "Dropped an Ampio message that failed processing "
                    "(topic %s, payload %.200r); the connection stays up",
                    topic,
                    payload,
                )
            return
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
    def objects(self) -> Mapping[int, AmpioObject]:
        """All known objects keyed by id.

        A read-only live view of frozen instances: it always reflects the
        store's current state, and neither the mapping nor an object in it
        can be mutated from consumer code.
        """
        return MappingProxyType(self._store.objects)

    @property
    def modules(self) -> Mapping[int, AmpioModule]:
        """All known physical modules keyed by id, as a read-only live view."""
        return MappingProxyType(self._store.modules)

    @property
    def server_info(self) -> AmpioServerInfo | None:
        """The Ampio M-SERV self-reported info, if discovered.

        Guaranteed non-None once :meth:`wait_for_initial_discovery` has
        returned True; every held info carries a populated
        :pyattr:`AmpioServerInfo.key` by construction.
        """
        return self._store.server_info

    @property
    def mserv(self) -> AmpioModule | None:
        """The M-SERV's own module row, for naming the hub device.

        Prefers cross-validating the server's self-reported mac against
        each module's mac_global/mac; falls back to the unique hub-typed
        module. None when neither identifies one - ambiguity included -
        and always None on the restricted tier, which never receives the
        module catalogue. Tier-independent device grouping needs no module
        row at all: see :pyattr:`AmpioObject.is_server_owned`.
        """
        info = self._store.server_info
        if info is not None:
            for mod in self._store.modules.values():
                if info.mac in (mod.mac_global, mod.mac):
                    return mod
        candidates = [mod for mod in self._store.modules.values() if is_hub(mod.type)]
        if len(candidates) == 1:
            return candidates[0]
        return None

    @property
    def available(self) -> bool:
        """Whether the broker connection is up."""
        return self._connection.available

    @property
    def auth_failure(self) -> str | None:
        """Why the connection stopped, if the broker rejected the credentials.

        ``None`` while the credentials are accepted, including through outages
        the client is still trying to reconnect across. Once set, the
        connection loop has stopped for good. A fresh ``start()`` clears it
        but retries the constructor credentials; recovering from a genuinely
        changed password means building a new client.
        """
        return self._connection.auth_failure

    @property
    def access_tier(self) -> AccessTier:
        """The account tier: the reserved ``admin`` login, or restricted.

        Decided by the authenticated username at construction - see
        :class:`AccessTier` for what each tier is served. A config flow can
        read the wire's own confirmation from :meth:`test_connection`'s
        result (:pyattr:`AmpioServerInfo.access_tier`) before any client
        exists.
        """
        return self._tier

    @property
    def last_payloads(self) -> dict[str, str]:
        """Verbatim last response payload per endpoint, keyed by endpoint name.

        Retained for the HA integration's diagnostics blob so a report can
        include the actual JSON the M-SERV emitted. Keyed by endpoint name,
        covering the endpoints the account tier is served; an endpoint is
        absent until its first reply lands. A payload that failed to parse
        is retained too - the bad bytes are exactly what a diagnostics
        report needs.
        """
        return {
            name: channel.last_payload
            for name, channel in self._channels.items()
            if channel.last_payload is not None
        }

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

        Everything the client learns flows through this one stream in the
        order it was produced - :mod:`ampio_mqtt.events` documents each
        event class, its ordering guarantees, and which account tiers
        produce it. ``of`` narrows the subscription to one event class
        (which also types the callback parameter precisely) or a tuple of
        classes::

            client.subscribe(on_any_event)
            client.subscribe(on_object, of=ObjectUpdated)
            client.subscribe(on_gone, of=(ObjectRemoved, ModuleRemoved))

        The returned unsubscribe removes exactly its own registration and
        is idempotent - consumer teardown lists routinely invoke a cleanup
        callback more than once, and the same listener registered twice
        keeps its other registration.
        """
        only = (of,) if isinstance(of, type) else of
        entry = (listener, only)
        self._listeners.append(entry)

        def _unsubscribe() -> None:
            self._listeners = [e for e in self._listeners if e is not entry]

        return _unsubscribe

    @staticmethod
    async def test_connection(
        host: str,
        username: str,
        password: str | None,
        *,
        port: int = 1883,
        info_timeout: float = 5.0,
        mqtt_client_factory: _connection.MqttClientFactory | None = None,
    ) -> AmpioServerInfo:
        """Connect, request the server info, and return it.

        Raises ``AmpioAuthError`` on credential rejection, ``AmpioTimeoutError``
        when the connection succeeds but no parseable info reply arrives
        within ``info_timeout`` (slow or overloaded broker - worth retrying),
        and ``AmpioConnectionError`` on any other connection failure. A reply
        without the server identity is unparseable like any other corrupt
        shape, so a returned info always has a populated
        :pyattr:`AmpioServerInfo.key` for the config flow's unique id.

        The result's ``access_tier`` tells a config flow what the account
        will get before any client exists: a ``RESTRICTED`` account never
        receives the module list, so a consumer that needs ``modules`` or
        ``mserv`` can reject it here with an accurate message instead of
        failing at setup.
        """
        if not username:
            raise ValueError(
                "username is required - the Ampio topics are namespaced by account"
            )
        info = ENDPOINT_BY_NAME["info"]
        payload = await _connection.probe(
            host,
            port,
            username,
            password,
            request_topic=request_topic(info, username),
            request_payload=info.req_payload,
            reply_topic=response_topic(info, username),
            timeout=info_timeout,
            client_factory=mqtt_client_factory,
        )
        if payload is None:
            raise AmpioTimeoutError(
                f"No server-info reply from the Ampio broker within {info_timeout}s"
            )
        parsed = _protocol.parse_server_info(payload)
        if parsed is None:
            # A corrupt reply gets the same retryable shape as silence:
            # something answered, but not with an info document.
            raise AmpioTimeoutError(
                "The Ampio broker answered with an unparseable server-info reply"
            )
        _protocol.warn_if_below_baseline(parsed.server_version)
        return parsed

    async def start(
        self, *, timeout: float = 15.0, discovery_timeout: float = 8.0
    ) -> bool:
        """Start the connection, wait for connect and initial discovery.

        After connecting, waits up to `discovery_timeout` for the initial
        object catalogue so names and classification are known before
        entities are created; which catalogue pair completes it depends on
        the account tier - see :meth:`wait_for_initial_discovery`. Calling
        ``start()`` on a running client closes the previous session first
        and starts over.

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

        Waits for the tier's initial replies: the states snapshot, the
        server info, and the account's object catalogue pair - the admin
        ``config`` pair (devicesDetails -> ``objects``, devices ->
        ``modules``) or the app-sync ``data`` pair (data/devices ->
        grant-filtered ``objects``, data/params_devices -> visibility
        flags). Returns True on completion and False if ``timeout``
        elapses first.

        This is the contract a consumer relies on when it must read
        ``objects``/``server_info`` (and, on the admin tier, ``modules``)
        before building anything on top of the client. A True additionally
        guarantees the server identity: ``server_info`` is populated, and
        :pyattr:`AmpioServerInfo.key` is a string by construction. It never
        raises on timeout - discovery continues opportunistically and this
        simply returns False.

        Safe to call repeatedly and after reconnects - the underlying signals
        latch on first completion, so once discovery has happened this returns
        immediately.
        """
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(
                    *(
                        self._channels[name].received.wait()
                        for name in self._initial_endpoints
                    )
                )
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

    async def refresh(self) -> None:
        """Re-request the tier's initial-discovery set.

        ``start()`` issues this once on every (re)connect; call it to force
        a fresh discovery cycle without reconnecting.
        """
        for name in self._initial_endpoints:
            await self._publish(ENDPOINT_BY_NAME[name])

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
        replies = await self._fetch(
            ("groups", "group_devices"),
            timeout,
            "Timed out fetching room map from Ampio broker",
        )
        return _protocol.parse_rooms(replies["groups"], replies["group_devices"])

    async def fetch_scenes(self, timeout: float = 5.0) -> list[AmpioScene]:
        """Return the scene catalogue defined in the Ampio app.

        Requires ``start()`` to have completed. Raises ``AmpioConnectionError``
        if the broker is not connected and ``AmpioTimeoutError`` if the
        response does not arrive within ``timeout``.
        """
        replies = await self._fetch(
            ("scenes",), timeout, "Timed out fetching scenes from Ampio broker"
        )
        # The channel resolved with the scenes endpoint's own parser output;
        # the cast recovers the type the table's Callable field erases.
        return cast("list[AmpioScene]", replies["scenes"])

    async def send_event(self, event_number: int) -> None:
        """Raise a bus event, running whatever Ampio logic is bound to it.

        Works on both account tiers and is bounded by nothing - see the
        bus-events section of docs/protocol.md for the rights model.
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
        """Publish a scene command; the M-SERV replays the scene's own
        actions, grant-scoped like any other command (docs/protocol.md)."""
        await self._connection.publish(
            command_topic(self._username), scene_payload(scene_id, verb).encode()
        )

    # --- commands ---------------------------------------------------------

    async def command(
        self, object_id: int, verb: str, *args: object, confirm: float | None = None
    ) -> AmpioObject | None:
        """Send ``verb`` (with any args) to an object on the command surface.

        Publishes ``/api/set/<object_id>/<verb>[/<arg>...]`` at QoS 1 and
        returns once the broker acknowledges it - "the broker accepted the
        command", not "the M-SERV applied it"; the resulting state arrives
        through the normal object listeners. This is the escape hatch for
        the verbs the library does not wrap - docs/protocol.md carries the
        verb table, the grant-scoping rules, and what the M-SERV silently
        ignores.

        ``confirm`` opts into awaiting that state: the call returns the
        snapshot of the next :class:`~ampio_mqtt.events.ObjectUpdated` for
        the object within ``confirm`` seconds, raising ``AmpioTimeoutError``
        on expiry. The `/api` surface has no reply topic, so the echo is an
        observation and nothing stronger: an update for the object arrived
        after the publish - a concurrent change from another source
        satisfies it just as well. A timeout, conversely, is how every
        silent drop finally surfaces: a verb the M-SERV ignores, an object
        outside the account's grant, or a command whose effect left the
        state unchanged and pushed nothing. Most verbs echo on the
        per-object path in under ~200 ms and `arm`/`disarm` take ~1 s
        (docs/protocol.md), so ``confirm=2.0`` covers the measured surface.
        The waiter is armed before the publish, so an echo cannot slip past
        the wait however fast it lands. Scene commands and
        :meth:`send_event` fan out beyond a single object and offer no
        per-object echo to await.

        Raises ``AmpioConnectionError`` when the broker is unreachable and
        ``AmpioTimeoutError`` when it fails to acknowledge in time; never an
        aiomqtt exception type.
        """
        payload = command_payload(object_id, verb, args).encode()
        if confirm is None:
            await self._connection.publish(command_topic(self._username), payload)
            return None
        future: asyncio.Future[AmpioObject] = asyncio.get_running_loop().create_future()

        def _echo(event: ObjectUpdated) -> None:
            if event.object.id == object_id and not future.done():
                future.set_result(event.object)

        unsubscribe = self.subscribe(_echo, of=ObjectUpdated)
        try:
            # The publish sits inside the window so `confirm` bounds the
            # whole call, exactly as `_fetch` bounds its own publishes.
            async with asyncio.timeout(confirm):
                await self._connection.publish(command_topic(self._username), payload)
                return await future
        except TimeoutError as err:
            raise AmpioTimeoutError(
                f"No state echo for object {object_id} within {confirm}s - "
                f"the M-SERV ignored {verb!r}, the object is outside this "
                "account's grant, or the command changed nothing"
            ) from err
        finally:
            unsubscribe()

    async def turn_on(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Turn an object fully on.

        Raises ``ValueError`` for an output whose kind says the switch verbs
        do not apply (``rgbw``): turning a color light on means choosing a
        color - the consumer's call, via :meth:`set_color` (the rgbw
        replay pattern in docs/protocol.md). ``confirm`` awaits the state
        echo exactly as :meth:`command` documents.
        """
        self._check_switchable(object_id, "turnOn")
        return await self.command(object_id, "turnOn", confirm=confirm)

    async def turn_off(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Turn an object off.

        A color output that does not answer the switch verbs (``rgbw``) is
        turned off with ``setColors 0/0/0/0`` instead - off is unambiguous,
        so the library routes it. An object whose kind is not yet known gets
        the plain verb. ``confirm`` awaits the state echo exactly as
        :meth:`command` documents.
        """
        kind = self._output_kind(object_id)
        if kind is not None and not kind.switchable and kind.color:
            return await self.set_color(object_id, 0, 0, 0, 0, confirm=confirm)
        return await self.command(object_id, "turnOff", confirm=confirm)

    async def toggle(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Invert an object's current on/off state.

        Raises ``ValueError`` for an output whose kind says the switch verbs
        do not apply (``rgbw``), exactly as :meth:`turn_on` does. ``confirm``
        awaits the state echo exactly as :meth:`command` documents.
        """
        self._check_switchable(object_id, "switch")
        return await self.command(object_id, "switch", confirm=confirm)

    def _output_kind(self, object_id: int) -> OutputKind | None:
        """The object's kind when it is a known output, else None."""
        obj = self._store.objects.get(object_id)
        kind = obj.kind if obj is not None else None
        return kind if isinstance(kind, OutputKind) else None

    def _check_switchable(self, object_id: int, verb: str) -> None:
        """Reject a switch-family verb for an output known not to answer it -
        the M-SERV would drop it with no effect and no reply. An object
        with no metadata yet passes through."""
        kind = self._output_kind(object_id)
        if kind is not None and not kind.switchable:
            raise ValueError(
                f"object {object_id} ({kind.key}) does not answer {verb}; "
                "drive it with set_color()"
            )

    async def set_value(
        self,
        object_id: int,
        value: int,
        *,
        pulse_ms: int | None = None,
        confirm: float | None = None,
    ) -> AmpioObject | None:
        """Set an object's 0-255 level (relay, flag, dimmer).

        With ``pulse_ms`` the M-SERV reverts the object to its previous state
        after that many milliseconds - a timed pulse, not a fade. The wire unit
        is 10 ms, so the value is rounded down to the nearest 10 ms; a gate
        pulse of 500 ms is ``pulse_ms=500``. ``confirm`` awaits the state
        echo exactly as :meth:`command` documents - for a pulse that is the
        set edge, not the later revert.
        """
        _check_range("value", value, 0, 255)
        if pulse_ms is None:
            return await self.command(object_id, "setValue", value, confirm=confirm)
        _check_range("pulse_ms", pulse_ms, 0, 655350)
        return await self.command(
            object_id, "setValue", value, pulse_ms // 10, confirm=confirm
        )

    async def set_temperature(
        self, object_id: int, temperature: float, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Set a thermostat's (``reg``) target temperature in °C.

        The library surfaces only the regulator's running flag until the
        climate readback lands (#73). Bools and non-finite floats are
        rejected: both would serialize as text the M-SERV silently drops.
        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
        ):
            raise ValueError(
                f"temperature must be a finite number, got {temperature!r}"
            )
        return await self.command(
            object_id, "setTemperature", temperature, confirm=confirm
        )

    async def set_color(
        self,
        object_id: int,
        red: int,
        green: int,
        blue: int,
        white: int = 0,
        *,
        confirm: float | None = None,
    ) -> AmpioObject | None:
        """Set an RGBW object's four channels, each 0-255.

        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        for name, channel in (
            ("red", red),
            ("green", green),
            ("blue", blue),
            ("white", white),
        ):
            _check_range(name, channel, 0, 255)
        return await self.command(
            object_id, "setColors", red, green, blue, white, confirm=confirm
        )

    async def open_cover(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Drive a cover to fully open (position 100).

        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        return await self.command(object_id, "open", confirm=confirm)

    async def close_cover(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Drive a cover to fully closed (position 0).

        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        return await self.command(object_id, "close", confirm=confirm)

    async def stop_cover(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Halt a cover wherever it is, on either axis - a stationary cover
        is a silent no-op. The `stop` row of docs/protocol.md details the
        mid-travel and mid-rotation behavior. ``confirm`` awaits the state
        echo exactly as :meth:`command` documents."""
        return await self.command(object_id, "stop", confirm=confirm)

    async def set_cover_position(
        self,
        object_id: int,
        position: int,
        *,
        lamella: int | None = None,
        confirm: float | None = None,
    ) -> AmpioObject | None:
        """Drive a cover to ``position`` percent (0 closed, 100 open).

        ``lamella`` sets the slat angle of a blind that has one, in the same
        command; omitting it sends no angle, which lets travel drag the
        slats along mechanically - pass it to land on a chosen angle (the
        slat-drag note in docs/protocol.md). Position updates stream in as
        the cover travels; ``confirm`` awaits the first of them exactly as
        :meth:`command` documents, so its snapshot reads the travel's start,
        not its end.
        """
        _check_range("position", position, 0, 100)
        if lamella is not None:
            _check_range("lamella", lamella, 0, 100)
        return await self.command(
            object_id,
            "setRollerPos",
            position,
            KEEP_POSITION if lamella is None else lamella,
            confirm=confirm,
        )

    async def set_cover_tilt(
        self, object_id: int, lamella: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Set a blind's slat angle percent, leaving its position alone.

        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        _check_range("lamella", lamella, 0, 100)
        return await self.command(
            object_id, "setRollerPos", KEEP_POSITION, lamella, confirm=confirm
        )

    async def _publish(self, ep: Endpoint) -> None:
        """Publish an endpoint's request keyword to its control topic."""
        await self._connection.publish(
            request_topic(ep, self._username), ep.req_payload.encode()
        )

    async def _fetch(
        self, names: tuple[str, ...], timeout: float, timeout_message: str
    ) -> dict[str, Any]:
        """Request the given endpoints and return each parsed reply by name.

        One future per endpoint correlates this caller with the next parseable
        reply, so concurrent fetches never disturb each other and the
        discovery latches stay untouched; every concurrent caller of the same
        endpoint receives the same reply, already parsed by the endpoint's
        own gate. The wire carries no correlation ids - a reply already in
        flight from an earlier request can satisfy a later ask, which for
        these idempotent read endpoints is the intended semantics. A reply
        that does not parse resolves nothing (see ``_handle_message``), so a
        corrupt reply ends in the same retryable ``AmpioTimeoutError`` as no
        reply at all.
        """
        loop = asyncio.get_running_loop()
        futures: dict[str, asyncio.Future[Any]] = {
            name: loop.create_future() for name in names
        }
        for name, future in futures.items():
            self._channels[name].waiters.append(future)
        try:
            # The publishes sit inside the window so `timeout` bounds the
            # whole call: each one otherwise awaits its PUBACK under the
            # connection's own deadline, and a slow-to-ack broker would
            # stretch a "5 s" fetch to three times that before the reply
            # wait even began.
            async with asyncio.timeout(timeout):
                for name in names:
                    await self._publish(ENDPOINT_BY_NAME[name])
                await asyncio.gather(*futures.values())
        except TimeoutError as err:
            raise AmpioTimeoutError(timeout_message) from err
        finally:
            # A resolved future was already dropped by deliver(); on timeout,
            # cancellation, or a failed publish, remove this call's own
            # waiters so a late reply resolves nothing stale.
            for name, future in futures.items():
                waiters = self._channels[name].waiters
                if future in waiters:
                    waiters.remove(future)
        return {name: future.result() for name, future in futures.items()}


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
