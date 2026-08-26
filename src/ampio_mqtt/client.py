"""Async MQTT client for the Ampio DB-object protocol.

See ``docs/discovery-flow.md`` for the ``start()`` lifecycle and what
runs automatically vs on demand.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from types import MappingProxyType
from typing import Any, Final, TypeVar, cast, overload

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
from .errors import AmpioConnectionError, AmpioTimeoutError
from .events import (
    AuthFailed,
    AvailabilityChanged,
    ClientEvent,
    ConnectionDied,
    ObjectRemoved,
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

# The regulator mode letters `setHeatingMode` accepts (docs/protocol.md);
# the readback letter is `ThermostatState.mode`.
HEATING_MODES: Final[frozenset[str]] = frozenset({"A", "S", "M", "H"})

EventListener = Callable[[ClientEvent], None]
_EventT = TypeVar("_EventT", bound=ClientEvent)
_EventT1 = TypeVar("_EventT1", bound=ClientEvent)
_EventT2 = TypeVar("_EventT2", bound=ClientEvent)

# Bounded so an `object_id` filter on a class without `.object` fails to
# type-check, mirroring the runtime ValueError.
_ObjEventT = TypeVar("_ObjEventT", bound=ObjectUpdated | ObjectRemoved)
_ObjEventT1 = TypeVar("_ObjEventT1", bound=ObjectUpdated | ObjectRemoved)
_ObjEventT2 = TypeVar("_ObjEventT2", bound=ObjectUpdated | ObjectRemoved)

# One registration in either listener registry: (listener, event-type filter).
_ListenerEntry = tuple[Callable[[Any], None], tuple[type[ClientEvent], ...] | None]


class _ReplyChannel:
    """One endpoint's reply tracking.

    ``received`` latches on the first parsed reply and never clears;
    ``last_payload`` keeps the verbatim payload for diagnostics;
    ``waiters`` are fetch futures awaiting the next parsed reply.
    """

    __slots__ = ("last_payload", "received", "waiters")

    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.last_payload: str | None = None
        self.waiters: list[asyncio.Future[Any]] = []

    def deliver(self, payload: str, parsed: object | None) -> None:
        """Record one reply; ``parsed`` is None when the payload could not
        be read. A malformed reply neither latches discovery nor resolves
        a waiter - the fetch times out into the same retryable error as
        silence - but its bytes still land in ``last_payload``."""
        self.last_payload = payload
        if parsed is None:
            return
        self.received.set()
        waiters, self.waiters = self.waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(parsed)

    def record(self, payload: str, parsed_ok: bool) -> None:
        """Record a store-gated reply: latch discovery when it parsed and
        keep the verbatim payload either way. These endpoints produce no
        fetchable value, so no waiter is resolved - ``_fetch`` rejects
        their names outright."""
        self.last_payload = payload
        if parsed_ok:
            self.received.set()


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
        refresh_interval: float | None = None,
        mqtt_client_factory: _connection.MqttClientFactory | None = None,
    ) -> None:
        """Initialize the client. `username` names the Ampio account and
        namespaces every MQTT topic; an empty one is rejected here.

        ``refresh_interval`` opts into a periodic re-request of the
        tier's discovery set, in seconds; None (the default) leaves the
        cadence to the consumer. Each cycle re-publishes the
        initial-discovery requests, so Designer additions and evictions
        surface as :class:`ObjectAdded` / :class:`ObjectRemoved` without
        a reconnect (#80).

        ``mqtt_client_factory`` is the transport seam: a zero-argument
        callable returning the MQTT session object for one connect
        attempt. Leave it None for the real broker connection; a test
        injects a fake broker instance here.
        """
        if not username:
            raise ValueError(
                "username is required - the Ampio topics are namespaced by account"
            )
        if reconnect_interval <= 0:
            raise ValueError("reconnect_interval must be positive seconds")
        if refresh_interval is not None and refresh_interval <= 0:
            raise ValueError("refresh_interval must be positive seconds or None")
        self._refresh_interval = refresh_interval
        self._refresh_task: asyncio.Task[None] | None = None
        self._username = username
        # The tier (see AccessTier) shapes the endpoints served, and from
        # them the subscriptions, router, reply channels, and the
        # initial-discovery set.
        self._tier = (
            AccessTier.ADMIN if username == ADMIN_USERNAME else AccessTier.RESTRICTED
        )
        self._served = tuple(ep for ep in ENDPOINTS if ep.tier in (None, self._tier))
        self._initial_endpoints = tuple(ep.name for ep in self._served if ep.initial)
        self._router = _protocol.Router(username, self._served)
        self._store = AmpioStore()
        self._stats = ConnectionStats()
        self._connection = _connection.Connection(
            host,
            port,
            username,
            password,
            reconnect_interval=reconnect_interval,
            topics=self._subscriptions(),
            stats=self._stats,
            on_message=self._handle_message,
            on_availability=self._handle_availability,
            on_connected=self.refresh,
            on_auth_failure=self._handle_auth_failure,
            on_fatal=self._handle_fatal,
            client_factory=mqtt_client_factory,
        )

        # The class-filtered list every event walks, and the per-object
        # buckets that make one-listener-per-object dispatch O(1) (#99).
        self._listeners: list[_ListenerEntry] = []
        self._by_object: dict[int, list[_ListenerEntry]] = {}

        # One reply channel per served endpoint; the router covers the
        # same set, so every routed reply has a channel.
        self._channels: dict[str, _ReplyChannel] = {
            ep.name: _ReplyChannel() for ep in self._served
        }

        # Topics whose messages have failed processing, so a recurring
        # poison payload logs its traceback once instead of per delivery.
        self._poisoned_topics: set[str] = set()

        # Per-mac futures awaiting a device_api info reply; every waiter
        # for a mac receives the same reply, exactly as endpoint fetches
        # share one.
        self._descriptions_waiters: dict[
            int, list[asyncio.Future[tuple[_protocol.OutputDescription, ...]]]
        ] = {}

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
                _protocol.DEVICE_API_INFO_WILDCARD,
            ]
        return topics

    def _handle_message(self, topic: str, payload: str) -> None:
        """Apply one message, then dispatch what it changed.

        Guarded per message: a processing bug costs the one message that
        triggered it, never the connection. The traceback logs once per
        topic and repeats at debug; bugs in the connection loop itself
        remain terminal.
        """
        self._stats.last_message_at = time.time()
        try:
            msg = self._router.route(topic, payload)
            if msg is None:
                return
            if (
                isinstance(msg, _protocol.EndpointReply)
                and msg.endpoint.parses is not None
            ):
                # Pure request/response: the endpoint's parser runs once
                # and its output is what a fetch returns; nothing here
                # mutates the store.
                parsed = msg.endpoint.parses(payload)
                if parsed is None:
                    _LOGGER.warning("Could not parse Ampio %s reply", msg.endpoint.name)
                self._channels[msg.endpoint.name].deliver(payload, parsed)
                return
            if isinstance(msg, _protocol.DeviceDescriptions):
                for future in self._descriptions_waiters.pop(msg.mac, []):
                    future.set_result(msg.entries)
                return
            applied = self._store.apply(msg)
            if isinstance(msg, _protocol.EndpointReply):
                self._channels[msg.endpoint.name].record(payload, applied.parsed)
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
        """Hand `event` to the class-filtered registry, then to the bucket
        for its object id. Each listener sees its events in production
        order."""
        self._dispatch_to(self._listeners, event)
        if isinstance(event, ObjectUpdated | ObjectRemoved):
            bucket = self._by_object.get(event.object.id)
            if bucket is not None:
                self._dispatch_to(bucket, event)

    @staticmethod
    def _dispatch_to(entries: list[_ListenerEntry], event: ClientEvent) -> None:
        """Walk one registry copy; a listener that raises is logged and
        the rest still run. The copy pins this dispatch's audience: a
        listener registered mid-dispatch must not receive the in-flight
        event."""
        for listener, only in list(entries):
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

    def module_for(self, obj: AmpioObject) -> AmpioModule | None:
        """The catalogue row of the module that owns ``obj``, mac-validated.

        Joins ``obj.device_id`` to the module list, gated on the row's
        mac agreeing with the object's leaf-derived
        :pyattr:`AmpioObject.module_mac` - DB ids are volatile across a
        module replacement while the leaf mac is the stable identity
        (docs/identity.md). None when either side is missing or they
        disagree, and always on the restricted tier, which never receives
        the module catalogue; tier-independent grouping reads
        ``module_mac`` directly.
        """
        if obj.device_id is None:
            return None
        module = self._store.modules.get(obj.device_id)
        if module is None or module.mac is None or module.mac != obj.module_mac:
            return None
        return module

    @property
    def available(self) -> bool:
        """Whether the broker connection is up."""
        return self._connection.available

    @property
    def access_tier(self) -> AccessTier:
        """The account tier, decided by the authenticated username at
        construction - see :class:`AccessTier`. Before any client exists,
        a config flow reads :pyattr:`AmpioServerInfo.access_tier` from a
        :meth:`test_connection` result instead."""
        return self._tier

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """One credential-free report of the client's health.

        The dict a bug report or a consumer diagnostics platform can emit
        as-is: it carries no host, username, or password. Keys:

        - ``access_tier``: the account tier's value string.
        - ``available``: whether the broker connection is up.
        - ``auth_failure``: the broker's rejection reason once the
          connection loop has stopped for auth, else None.
        - ``server_info``: the safe self-report subset as a dict
          (:class:`AmpioServerInfo` excludes the private fields by
          construction), or None before discovery.
        - ``connection``: the run's liveness counters. ``started_at`` and
          ``reconnect_count`` cover the current ``start()`` run, so a
          deliberate restart never reads as a flapping connection;
          ``last_error`` and ``last_message_at`` roll across runs.
          ``subscribe_failures`` maps each topic the broker rejected in
          the latest SUBACK to its reason code.
        - ``mac_collisions``: override macs shared by two or more module
          rows, on which raw traffic cannot be attributed reliably.
        - ``last_payloads``: each endpoint's verbatim last reply, absent
          until one lands. A payload that failed to parse is retained
          too - the bad bytes are what the report needs.
        """
        server_info = self._store.server_info
        return {
            "access_tier": self._tier.value,
            "available": self.available,
            "auth_failure": self._connection.auth_failure,
            "server_info": None if server_info is None else asdict(server_info),
            "connection": {
                "started_at": self._stats.started_at,
                "reconnect_count": self._stats.reconnect_count,
                "last_message_at": self._stats.last_message_at,
                "last_error": self._stats.last_error,
                "subscribe_failures": dict(self._stats.subscribe_failures),
            },
            "mac_collisions": sorted(self._store.colliding_macs),
            "last_payloads": {
                name: channel.last_payload
                for name, channel in self._channels.items()
                if channel.last_payload is not None
            },
        }

    @overload
    def subscribe(self, listener: EventListener) -> Callable[[], None]: ...

    @overload
    def subscribe(
        self, listener: Callable[[_EventT], None], *, of: type[_EventT]
    ) -> Callable[[], None]: ...

    # One tuple contract, two spellings: pyright infers the member union
    # for the variadic form; mypy joins the members up to the TypeVar
    # bound and needs the two-class arity form instead (#92). Beyond two
    # classes, type the listener as ``Callable[[ClientEvent], None]`` and
    # the variadic form matches in both.
    @overload
    def subscribe(
        self, listener: Callable[[_EventT], None], *, of: tuple[type[_EventT], ...]
    ) -> Callable[[], None]: ...

    @overload
    def subscribe(
        self,
        listener: Callable[[_EventT1 | _EventT2], None],
        *,
        of: tuple[type[_EventT1], type[_EventT2]],
    ) -> Callable[[], None]: ...

    # The object_id forms repeat the single/variadic/pair spellings with
    # the TypeVars bound to the object-bearing classes, so a filter on
    # anything else fails to type-check like it fails at runtime.
    @overload
    def subscribe(
        self,
        listener: Callable[[_ObjEventT], None],
        *,
        of: type[_ObjEventT],
        object_id: int,
    ) -> Callable[[], None]: ...

    @overload
    def subscribe(
        self,
        listener: Callable[[_ObjEventT], None],
        *,
        of: tuple[type[_ObjEventT], ...],
        object_id: int,
    ) -> Callable[[], None]: ...

    @overload
    def subscribe(
        self,
        listener: Callable[[_ObjEventT1 | _ObjEventT2], None],
        *,
        of: tuple[type[_ObjEventT1], type[_ObjEventT2]],
        object_id: int,
    ) -> Callable[[], None]: ...

    def subscribe(
        self,
        listener: Callable[[Any], None],
        *,
        of: type | tuple[type, ...] | None = None,
        object_id: int | None = None,
    ) -> Callable[[], None]:
        """Register ``listener`` on the event stream; returns an unsubscribe.

        Everything the client learns flows through this one stream in the
        order it was produced - :mod:`ampio_mqtt.events` documents each
        event class, its ordering guarantees, and which account tiers
        produce it. ``of`` narrows the subscription to one event class or
        a tuple of classes, typing the callback parameter as that class
        or union::

            client.subscribe(on_any_event)
            client.subscribe(on_object, of=ObjectUpdated)
            client.subscribe(on_gone, of=(ObjectRemoved, ModuleRemoved))

        Listeners are invoked synchronously on the asyncio event loop
        that ran :meth:`start`, never from another thread, so a listener
        can touch loop-bound state directly (#81).

        ``object_id`` narrows further, to one object's events. ID-filtered
        listeners live in per-object buckets, so dispatch reaches only the
        matching bucket, in O(1) of their total count (#99)::

            client.subscribe(on_object, of=ObjectUpdated, object_id=135)
            client.subscribe(on_135, of=(ObjectUpdated, ObjectRemoved),
                             object_id=135)

        Only :class:`ObjectUpdated` and :class:`ObjectRemoved` carry the
        ``.object`` an ID can filter on; ``object_id`` with any other
        class, or with no ``of`` at all, raises ``ValueError`` at
        registration time.

        The returned unsubscribe removes exactly its own registration and
        is idempotent; the same listener registered twice keeps its other
        registration.
        """
        only = (of,) if isinstance(of, type) else of
        if only is not None and not only:
            raise ValueError("of= must name at least one event class")
        if object_id is not None and (
            only is None
            or any(not issubclass(cls, ObjectUpdated | ObjectRemoved) for cls in only)
        ):
            raise ValueError(
                "object_id filters on event.object.id, so of= must name only "
                "ObjectUpdated and/or ObjectRemoved"
            )
        entry: _ListenerEntry = (listener, only)
        if object_id is None:
            self._listeners.append(entry)

            def _unsubscribe() -> None:
                self._listeners = [e for e in self._listeners if e is not entry]

        else:
            self._by_object.setdefault(object_id, []).append(entry)

            def _unsubscribe() -> None:
                bucket = self._by_object.get(object_id)
                if bucket is None:
                    return
                remaining = [e for e in bucket if e is not entry]
                if remaining:
                    self._by_object[object_id] = remaining
                else:
                    # The last registration takes the bucket with it, so
                    # entity churn cannot grow the dict without bound.
                    del self._by_object[object_id]

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

        Raises ``AmpioAuthError`` on credential rejection,
        ``AmpioTimeoutError`` (retryable) when the connection succeeds but
        no parseable info reply arrives within ``info_timeout``, and
        ``AmpioConnectionError`` on any other connection failure. A
        returned info always has a populated
        :pyattr:`AmpioServerInfo.key` for the config flow's unique id,
        and its ``access_tier`` tells the flow what the account will be
        served before any client exists.
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
        discovery cycle - see :meth:`wait_for_initial_discovery` for what
        completes it per tier. Calling ``start()`` on a running client
        closes the previous session first and starts over.

        Returns True when discovery completed in time and False when
        `discovery_timeout` elapsed first. A False leaves the connection
        up and discovery continuing; await
        :meth:`wait_for_initial_discovery` rather than restarting. A
        consumer that must read `modules`/`objects`/`server_info` before
        building on the client checks this result or awaits that method.
        """
        await self._connection.open(timeout)
        await self._cancel_refresh_task()
        if self._refresh_interval is not None:
            self._refresh_task = asyncio.get_running_loop().create_task(
                self._refresh_periodically(self._refresh_interval)
            )
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

        A True guarantees ``objects`` and ``server_info`` (and, on the
        admin tier, ``modules``) are populated, with
        :pyattr:`AmpioServerInfo.key` a string by construction. It never
        raises on timeout - discovery continues and this returns False.
        Safe to call repeatedly and after reconnects: the signals latch
        on first completion, so this then returns immediately.
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
        await self._cancel_refresh_task()
        await self._connection.close()

    async def _cancel_refresh_task(self) -> None:
        if self._refresh_task is None:
            return
        self._refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._refresh_task
        self._refresh_task = None

    async def _refresh_periodically(self, interval: float) -> None:
        """Re-run refresh() every `interval` seconds while the broker is up.

        An offline tick skips silently: the reconnect path refreshes on
        connect, so a periodic request adds nothing while the broker is
        away, and the drop that races the availability check surfaces as
        the connection error swallowed here for the same reason.
        """
        while True:
            await asyncio.sleep(interval)
            if not self.available:
                continue
            try:
                await self.refresh()
            except AmpioConnectionError:
                continue

    async def refresh(self) -> None:
        """Re-request the tier's initial-discovery set.

        ``start()`` issues this once on every (re)connect; call it to force
        a fresh discovery cycle without reconnecting. The call also resets
        the store's live-value protection, so the requested snapshot can
        correct values that only carry a local receive stamp.
        """
        self._store.begin_refresh()
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
        # The cast recovers the type the endpoint table's Callable field
        # erases; the copy keeps concurrent callers of one reply from
        # seeing each other's list mutations.
        return list(cast("list[AmpioScene]", replies["scenes"]))

    async def fetch_locations(self, timeout: float = 5.0) -> dict[int, str]:
        """Return ``{location_id: name}`` - the Designer "Lokalizacja" table.

        The name table the per-output location pointer resolves through;
        :meth:`resolve_locations` consumes it and per-object consumers read
        :pyattr:`AmpioObject.location` instead. Admin tier only - the
        ``config`` surface never answers a restricted account, and the call
        raises ``RuntimeError`` for one.

        Requires ``start()`` to have completed. Raises
        ``AmpioConnectionError`` if the broker is not connected and
        ``AmpioTimeoutError`` if the response does not arrive within
        ``timeout``.
        """
        replies = await self._fetch(
            ("locations",),
            timeout,
            "Timed out fetching the locations table from the Ampio broker",
        )
        return dict(cast("dict[int, str]", replies["locations"]))

    async def resolve_locations(self, timeout: float = 10.0) -> dict[int, str]:
        """Resolve every object's Designer location and return the map.

        Fetches the locations name table, asks each catalogued module for
        its CAN-resident description record over the ``device_api`` tree,
        joins the entries to objects, and folds the result into the store:
        :pyattr:`AmpioObject.location` carries the name afterwards, and a
        record's Matter tag refines :pyattr:`AmpioObject.matter_device_type`
        (#110). Changes dispatch as :class:`ObjectUpdated`.

        Returns ``{object_id: location_name}`` for what resolved. A module
        that does not answer within ``timeout`` is skipped without error -
        offline modules are normal - so the map can be partial; call again
        for another sweep. An object absent from a sweep's resolution keeps
        its previous ``location`` - a partial sweep never clears it - until
        a catalogue eviction or a later sweep that covers the object.

        The sweep waits out the full ``timeout`` whenever any module stays
        silent, which is normal on a real install, and the name table is
        fetched first on its own ``timeout`` budget, so the call can take
        up to twice ``timeout`` end to end.

        Admin tier only: the ``device_api`` tree answers no other account,
        and the call raises ``RuntimeError`` for one. Requires ``start()``
        to have completed. Raises ``AmpioConnectionError`` if the broker is
        not connected and ``AmpioTimeoutError`` if the name table itself
        does not arrive.
        """
        if self._tier is not AccessTier.ADMIN:
            raise RuntimeError(
                "resolve_locations() needs the reserved admin login - the "
                "device_api tree answers no other account"
            )
        names = await self.fetch_locations(timeout=timeout)
        macs = sorted(
            {mod.mac for mod in self._store.modules.values() if mod.mac is not None}
        )
        loop = asyncio.get_running_loop()
        futures: dict[int, asyncio.Future[tuple[_protocol.OutputDescription, ...]]] = {}
        for mac in macs:
            future = loop.create_future()
            futures[mac] = future
            self._descriptions_waiters.setdefault(mac, []).append(future)
        try:
            # The publishes sit inside the window, as _fetch's do; the
            # window elapsing is not an error - it bounds how long the
            # sweep waits for stragglers.
            async with asyncio.timeout(timeout):
                for mac in macs:
                    await self._connection.publish(
                        _protocol.device_api_request_topic(mac), b""
                    )
                await asyncio.gather(*futures.values())
        except TimeoutError:
            pass
        finally:
            for mac, future in futures.items():
                waiters = self._descriptions_waiters.get(mac)
                if waiters is not None:
                    waiters.remove(future)
                    if not waiters:
                        del self._descriptions_waiters[mac]
        by_mac = {
            mac: future.result()
            for mac, future in futures.items()
            if future.done() and not future.cancelled()
        }
        resolved = _protocol.resolve_designer(
            self._store.objects, by_mac, names, self._store.colliding_macs
        )
        applied = self._store.apply_designer_metadata(resolved)
        for event in applied.events:
            self._dispatch(event)
        return {
            oid: res.location
            for oid, res in resolved.items()
            if res.location is not None
        }

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
        snapshot of the next :class:`~ampio_mqtt.events.ObjectUpdated`
        for the object within ``confirm`` seconds, raising
        ``AmpioTimeoutError`` on expiry. The `/api` surface has no reply
        topic, so the echo is an observation, not an acknowledgment: a
        concurrent change satisfies it, and a timeout is how a silent
        drop surfaces (an ignored verb, an out-of-grant object, or a
        command that changed nothing). Most verbs echo in under ~200 ms
        and `arm`/`disarm` take ~1 s (docs/protocol.md), so
        ``confirm=2.0`` covers the measured surface. The waiter is armed
        before the publish. Scene commands and :meth:`send_event` fan out
        beyond a single object and offer no per-object echo.

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

        The regulator echoes the new target in its state push, readable
        as :attr:`AmpioObject.thermostat`. Bools and non-finite floats are
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

    async def set_heating_mode(
        self, object_id: int, mode: str, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Set a thermostat's (``reg``) operating mode.

        ``mode`` is one of :data:`HEATING_MODES` (``A``, ``S``, ``M``,
        ``H``), exactly as the wire spells it - `S` is the Designer's
        Schedule mode and `M` its Manual mode. The regulator echoes the
        letter in its state push, readable as
        :attr:`ThermostatState.mode`; an unlisted letter raises
        ``ValueError`` here rather than being dropped by the M-SERV
        (:meth:`command` is the escape hatch for experimenting).
        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        if mode not in HEATING_MODES:
            raise ValueError(
                f"mode must be one of {sorted(HEATING_MODES)}, got {mode!r}"
            )
        return await self.command(object_id, "setHeatingMode", mode, confirm=confirm)

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

        One future per endpoint awaits the next parseable reply; every
        concurrent caller of the same endpoint receives that same parsed
        reply. The wire carries no correlation ids - a reply already in
        flight from an earlier request can satisfy a later ask, which for
        these idempotent read endpoints is the intended semantics. A
        corrupt reply resolves nothing and ends in the same retryable
        ``AmpioTimeoutError`` as silence.
        """
        for name in names:
            if name not in self._channels:
                raise RuntimeError(
                    f"endpoint {name!r} is not served on the {self._tier.value} tier"
                )
            if ENDPOINT_BY_NAME[name].parses is None:
                raise RuntimeError(
                    f"endpoint {name!r} is store-gated and produces no "
                    "fetchable value; give it a parses gate to fetch it"
                )
        loop = asyncio.get_running_loop()
        futures: dict[str, asyncio.Future[Any]] = {
            name: loop.create_future() for name in names
        }
        for name, future in futures.items():
            self._channels[name].waiters.append(future)
        try:
            # The publishes sit inside the window so `timeout` bounds the
            # whole call, PUBACK waits included.
            async with asyncio.timeout(timeout):
                for name in names:
                    await self._publish(ENDPOINT_BY_NAME[name])
                await asyncio.gather(*futures.values())
        except TimeoutError as err:
            raise AmpioTimeoutError(timeout_message) from err
        finally:
            # Remove this call's remaining waiters so a late reply
            # resolves nothing stale.
            for name, future in futures.items():
                waiters = self._channels[name].waiters
                if future in waiters:
                    waiters.remove(future)
        return {name: future.result() for name, future in futures.items()}


def _check_range(name: str, value: int, low: int, high: int) -> None:
    """Reject a mis-typed or out-of-range command argument before the wire.

    Rejects bool explicitly: it passes ``isinstance(int)``, but the wire
    encoding is ``str()``, so a bool would go out as the literal ``True``
    - a malformed command the M-SERV silently drops.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an int in {low}..{high}, got {value!r}")
