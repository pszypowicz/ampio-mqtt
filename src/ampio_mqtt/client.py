"""Async MQTT client for the Ampio DB-object protocol.

See ``docs/discovery-flow.md`` for the ``connect()`` lifecycle and what
runs automatically vs on demand.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from types import MappingProxyType
from typing import Any, Final, TypeVar, cast, overload

from . import _connection, _protocol
from ._protocol import (
    ADMIN_ENDPOINTS,
    BASE_ENDPOINTS,
    ENDPOINT_BY_NAME,
    KEEP_POSITION,
    PANEL_MASK_MAX_BYTES,
    RAW_ANALOG_WILDCARD,
    RAW_BUZZER_OFF,
    RAW_BUZZER_SILENCE,
    RAW_COLOR_TEMP_WILDCARDS,
    RAW_DIAGNOSTICS_WILDCARD,
    RAW_EVENT_WILDCARD,
    RAW_IDENTIFY_OFF,
    RAW_IDENTIFY_ON,
    RAW_INPUT_WILDCARDS,
    RAW_OUTPUT_FUNCTION_BY_SF,
    RAW_OUTPUT_WILDCARD,
    ROLLER_BLOCK_CLOSING,
    ROLLER_BLOCK_OPENING,
    Endpoint,
    LeafFault,
    account_free_text,
    account_free_topic,
    command_payload,
    command_topic,
    event_payload,
    joins_roller_records,
    notification_payload,
    ob_state_wildcard,
    panel_field_mask,
    raw_backlight_payload,
    raw_buzzer_pattern_payload,
    raw_buzzer_payload,
    raw_key_lock_payload,
    raw_output_payload,
    raw_roller_lock_payload,
    raw_status_light_payload,
    raw_write_topic,
    request_topic,
    response_topic,
    scene_payload,
)
from ._store import AdminStore, AmpioStore, Applied
from .classification import InputKind, OutputKind
from .errors import (
    AmpioConnectionError,
    AmpioNotConfigured,
    AmpioProtocolError,
    AmpioTimeoutError,
    AmpioUnsupported,
    AmpioValueError,
)
from .events import (
    AuthFailed,
    AvailabilityChanged,
    ClientEvent,
    ConnectionDied,
    ObjectRemoved,
    ObjectUpdated,
    RecordSweepCompleted,
)
from .models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    ConnectionStats,
    CoverParameters,
    DesignerRecord,
    LockRefusal,
    LockTarget,
    ModuleFunction,
    ModuleRecord,
    PanelSettings,
    RecordSweep,
    format_mac,
)

_LOGGER = logging.getLogger(__name__)

# The regulator mode letters `setHeatingMode` accepts (docs/commands.md);
# the readback letter is `ThermostatState.mode`.
HEATING_MODES: Final[frozenset[str]] = frozenset({"A", "S", "M", "H"})

# The highest touch field a panel write can address, as the mask the frame
# carries allows. A consumer validates a field number against this before
# it calls, so a bad one never reaches the same rejection an unknown module
# raises (#220).
MAX_PANEL_FIELD: Final[int] = PANEL_MASK_MAX_BYTES * 8

# The refusal message for each way lock_target() can decline, keyed by the
# LockRefusal the four lock methods raise on.
_LOCK_REFUSALS: Final[dict[LockRefusal, str]] = {
    LockRefusal.NOT_A_COVER: (
        "not a cover in the roller description class (roleta_procenty, "
        "roleta_lamelki), and a non-cover's channel index can belong to a "
        "cover on the same module"
    ),
    LockRefusal.NO_ROLLER_COUNT: (
        "its module advertises no roller channel count and drops the lock frame"
    ),
    LockRefusal.PAST_LAST_CHANNEL: (
        "its channel lies past the roller count its module advertises"
    ),
}

EventListener = Callable[[ClientEvent], None]
_EventT = TypeVar("_EventT", bound=ClientEvent)
_EventT1 = TypeVar("_EventT1", bound=ClientEvent)
_EventT2 = TypeVar("_EventT2", bound=ClientEvent)

# Bounded so an `object_id` filter on a class without `.object` fails to
# type-check, mirroring the runtime AmpioValueError.
_ObjEventT = TypeVar("_ObjEventT", bound=ObjectUpdated | ObjectRemoved)
_ObjEventT1 = TypeVar("_ObjEventT1", bound=ObjectUpdated | ObjectRemoved)
_ObjEventT2 = TypeVar("_ObjEventT2", bound=ObjectUpdated | ObjectRemoved)

# One registration in either listener registry: (listener, event-type filter).
_ListenerEntry = tuple[Callable[[Any], None], tuple[type[ClientEvent], ...] | None]


# The server-info fields that identify the host the M-SERV runs on.
_HOST_IDENTIFIERS: Final = ("local_ip", "device_id")


def _masked_server_info(fields: dict[str, Any]) -> dict[str, Any]:
    """The server-info dict with each host identifier it carries masked."""
    return {
        key: _protocol.REDACTED
        if key in _HOST_IDENTIFIERS and value is not None
        else value
        for key, value in fields.items()
    }


def _retained(endpoint: _protocol.Endpoint, data: Mapping[str, Any]) -> str:
    """Retain an endpoint's safe copy or a summary of its rows."""
    redacts = endpoint.redacts or _protocol.summarize_rows
    return redacts(data)


class _ReplyChannel:
    """One endpoint's reply tracking.

    ``received`` latches on the first reply the parse accepted and never
    clears; ``last_payload`` keeps the safe reply summary for diagnostics;
    ``waiters`` are fetch futures awaiting the next accepted reply.
    """

    __slots__ = ("last_payload", "received", "waiters")

    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.last_payload: str | None = None
        self.waiters: list[asyncio.Future[Any]] = []

    def deliver(self, parsed: object) -> None:
        """Latch discovery and hand one parsed reply to every waiter."""
        self.latch()
        waiters, self.waiters = self.waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(parsed)

    def latch(self) -> None:
        """Mark this endpoint as answered.

        What a store-gated reply needs on its own: those endpoints produce
        no fetchable value, so no waiter is resolved - ``_fetch`` rejects
        their names outright.
        """
        self.received.set()


class AmpioClient:
    """The client every Ampio account gets: the account's own namespace.

    Holds the object catalogue, the server info, the event stream and the
    `/api` writes. :class:`AmpioAdminClient` extends it with what the
    M-SERV serves the reserved login alone. The class never inspects the
    username: the reserved login through this class gets the standard
    view.
    """

    # The three choices the admin subclass widens: the endpoints the
    # account is served, the store that applies them, and whether the
    # admin-only shapes (digests, device list, raw tree) are routed.
    _endpoints: tuple[Endpoint, ...] = BASE_ENDPOINTS
    _store_class: type[AmpioStore] = AmpioStore
    _routes_admin_shapes: bool = False

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
        ``port`` is the broker port. A None ``password`` connects with the
        username alone.

        ``refresh_interval`` opts into a periodic re-request of the
        discovery set, in seconds; None (the default) leaves the
        cadence to the consumer (docs/discovery-flow.md). Zero or
        negative raises ``AmpioValueError``.

        ``reconnect_interval`` is the reconnect backoff base, in seconds.
        Zero or negative raises ``AmpioValueError``.

        ``mqtt_client_factory`` is the transport seam: a zero-argument
        callable returning the MQTT session object for one connect
        attempt. Leave it None for the real broker connection; a test
        injects a fake broker instance here.
        """
        if not username:
            raise AmpioValueError(
                "username is required - the Ampio topics are namespaced by account"
            )
        if reconnect_interval <= 0:
            raise AmpioValueError("reconnect_interval must be positive seconds")
        if refresh_interval is not None and refresh_interval <= 0:
            raise AmpioValueError("refresh_interval must be positive seconds or None")
        self._refresh_interval = refresh_interval
        self._refresh_task: asyncio.Task[None] | None = None
        self._username = username
        self._host = host
        self._initial_endpoints = tuple(ep.name for ep in self._endpoints if ep.initial)
        self._router = _protocol.Router(
            username, self._endpoints, admin=self._routes_admin_shapes
        )
        self._store = self._store_class()
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
            on_connected=self._handle_connected,
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
            ep.name: _ReplyChannel() for ep in self._endpoints
        }

        # Topics whose messages have failed processing, so a recurring
        # poison payload logs its traceback once instead of per delivery.
        self._poisoned_topics: set[str] = set()

    def _subscriptions(self) -> list[tuple[str, int]]:
        """Every filter the client needs on each (re)connect, with its QoS.

        The account's endpoint replies and its per-object pushes, all at
        QoS 1. The admin client adds the digests and the raw tree.
        """
        live = [
            *(response_topic(ep, self._username) for ep in self._endpoints),
            ob_state_wildcard(self._username),
        ]
        return [(t, _connection.SUBSCRIBE_QOS) for t in live]

    def _handle_message(self, topic: str, payload: str, retained: bool = False) -> None:
        """Apply one message, then dispatch what it changed.

        ``retained`` marks a broker replay from its retained store rather
        than a live push. Guarded per message: a processing bug costs the
        one message that triggered it, never the connection. A reply the
        parse refuses is reported and dropped the same way. The traceback of
        anything else logs once per topic and repeats at debug; bugs in the
        connection loop itself remain terminal.
        """
        self._stats.last_message_at = time.time()
        try:
            msg = self._router.route(topic, payload)
            if msg is None:
                return
            if isinstance(msg, _protocol.EndpointReply):
                channel = self._channels[msg.endpoint.name]
                # One decode feeds the retained summary and whichever of the
                # two parse paths applies. A reply the decode refuses still
                # records receipt, and withholds every byte of a payload that
                # nothing could read.
                try:
                    data = _protocol.decode_envelope(payload, msg.endpoint.name)
                except AmpioProtocolError:
                    channel.last_payload = _protocol.REDACTED
                    raise
                channel.last_payload = _retained(msg.endpoint, data)
                if msg.endpoint.parses is not None:
                    # Pure request/response: the endpoint's parser runs once
                    # and its output is what a fetch returns; nothing here
                    # mutates the store.
                    channel.deliver(msg.endpoint.parses(data))
                    return
                applied = self._store.apply_endpoint(msg.endpoint, data)
                channel.latch()
            else:
                applied = self._apply_inbound(msg, retained)
        except LeafFault as err:
            # `leafId` rides `data/devices` alone, and the door that reads
            # it runs on whichever reply of the pair completes it. The
            # report names the reply that carried the row.
            self._note_protocol_violation(
                response_topic(ENDPOINT_BY_NAME["data_devices"], self._username), err
            )
            return
        except AmpioProtocolError as err:
            self._note_protocol_violation(topic, err)
            return
        except Exception:
            if topic in self._poisoned_topics:
                _LOGGER.debug("Dropped another failing Ampio message on %s", topic)
            else:
                self._poisoned_topics.add(topic)
                # The size stands in for the reply itself. A consumer
                # attaches its log to the same report as the diagnostics
                # download, and a table reply opens on the device names
                # that the retained summary leaves out.
                _LOGGER.exception(
                    "Dropped an Ampio message that failed processing "
                    "(topic %s, %d characters); the connection stays up",
                    topic,
                    len(payload),
                )
            return
        for event in applied.events:
            self._dispatch(event)

    def _apply_inbound(self, msg: _protocol.Inbound, retained: bool) -> Applied:
        """Apply one routed non-endpoint message. The admin client widens it."""
        return self._store.apply(msg, retained=retained)

    def _note_protocol_violation(self, topic: str, err: AmpioProtocolError) -> None:
        """Report a refused reply and keep the connection up.

        The reply lacked what its surface always serves, so the library refuses
        to read it (see :class:`AmpioProtocolError`). The report is this log
        line plus the ``protocol_violations`` entry a consumer can surface from
        :meth:`diagnostics_snapshot`. The entry is keyed on the masked topic,
        because a consumer publishes the snapshot. The log line keeps the real
        one, which the operator matches against the broker.
        """
        self._stats.protocol_violations[account_free_topic(topic)] = str(err)
        _LOGGER.error("Refused an Ampio reply on %s: %s", topic, err)

    def _handle_availability(self, available: bool) -> None:
        self._dispatch(AvailabilityChanged(available))

    async def _handle_connected(self) -> None:
        """The connection loop's on-connect step: request the discovery set."""
        await self.refresh()

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
    def server_info(self) -> AmpioServerInfo | None:
        """The Ampio M-SERV self-reported info, if discovered.

        Guaranteed non-None once :meth:`wait_for_initial_discovery` has
        returned True; every held info carries a populated
        :pyattr:`AmpioServerInfo.server_key` by construction.
        """
        return self._store.server_info

    @property
    def available(self) -> bool:
        """Whether the broker connection is up."""
        return self._connection.available

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """One credential-free report of the client's health.

        The dict a bug report or a consumer diagnostics platform can emit
        as-is: it carries no host, username, or password. Keys:

        - ``available``: whether the broker connection is up.
        - ``auth_failure``: the broker's rejection reason once the
          connection loop has stopped for auth, else None.
        - ``server_info``: the safe self-report subset as a dict
          (:class:`AmpioServerInfo` excludes the private fields by
          construction), with ``local_ip`` and ``device_id`` masked, or
          None before discovery.
        - ``connection``: the run's liveness counters. ``started_at`` and
          ``reconnect_count`` cover the current ``connect()`` run, so a
          deliberate restart never reads as a flapping connection;
          ``last_error`` and ``last_message_at`` roll across runs, and
          ``last_error`` masks the account segment of any topic it names
          and the broker host.
          ``subscribe_failures`` maps each topic the broker rejected in
          the latest SUBACK to its reason code.
          ``protocol_violations`` maps each topic whose reply the library
          refused to the reason, and rolls across runs. Both key on the
          topic with its account segment masked.
        - ``params_gap``: objects the ``params_devices`` table carries no
          row for. The table covers the whole catalogue on both tiers, so a
          non-empty list is a server fault: those objects read every
          Designer config flag as unset.
        - ``not_configured``: the ``(id, name)`` pairs of the catalogue
          rows the door left out because they carry no leaf.
        - ``last_payloads``: each endpoint's last reply summary (a row
          count, or the masked info reply), absent until a reply arrives
          (docs/discovery-flow.md).
        """
        server_info = self._store.server_info
        last_error = self._stats.last_error
        return {
            "available": self.available,
            "auth_failure": self._connection.auth_failure,
            "server_info": None
            if server_info is None
            else _masked_server_info(asdict(server_info)),
            "connection": {
                "started_at": self._stats.started_at,
                "reconnect_count": self._stats.reconnect_count,
                "last_message_at": self._stats.last_message_at,
                "last_error": None
                if last_error is None
                else account_free_text(last_error, self._host),
                "subscribe_failures": dict(self._stats.subscribe_failures),
                "protocol_violations": dict(self._stats.protocol_violations),
            },
            "params_gap": sorted(self._store.missing_params_ids),
            "not_configured": [list(pair) for pair in self._store.not_configured],
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
        that ran :meth:`connect`, never from another thread, so a listener
        can touch loop-bound state directly (#81).

        ``object_id`` narrows further, to one object's events::

            client.subscribe(on_object, of=ObjectUpdated, object_id=135)
            client.subscribe(on_135, of=(ObjectUpdated, ObjectRemoved),
                             object_id=135)

        Only :class:`ObjectUpdated` (and its :class:`ObjectAdded` subclass)
        and :class:`ObjectRemoved` carry the ``.object`` an ID can filter
        on; ``object_id`` with any other class, or with no ``of`` at all,
        raises ``AmpioValueError`` at registration time. An empty ``of``
        tuple raises ``AmpioValueError``.

        The returned unsubscribe removes exactly its own registration and
        is idempotent; the same listener registered twice keeps its other
        registration.
        """
        only = (of,) if isinstance(of, type) else of
        if only is not None and not only:
            raise AmpioValueError("of= must name at least one event class")
        if object_id is not None and (
            only is None
            or any(not issubclass(cls, ObjectUpdated | ObjectRemoved) for cls in only)
        ):
            raise AmpioValueError(
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
    async def check_connection(
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
        :pyattr:`AmpioServerInfo.server_key` for the config flow's unique
        id, and the tier it reports tells the flow which client class fits
        the account before any client exists. Raises ``AmpioValueError``
        for an empty username.
        """
        if not username:
            raise AmpioValueError(
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
        try:
            parsed = _protocol.parse_server_info(
                _protocol.decode_envelope(payload, info.name)
            )
        except AmpioProtocolError as err:
            # A refused reply gets the same retryable shape as silence:
            # something answered, but not with an info document.
            raise AmpioTimeoutError(
                "The Ampio broker answered with an unreadable server-info reply"
            ) from err
        _protocol.warn_if_below_baseline(parsed.server_version)
        return parsed

    async def connect(
        self, *, timeout: float = 15.0, discovery_timeout: float = 8.0
    ) -> bool:
        """Connect, wait for the connection and the initial discovery.

        After connecting, waits up to `discovery_timeout` for the initial
        discovery cycle - see :meth:`wait_for_initial_discovery` for what
        completes it. Calling ``connect()`` on a running client
        closes the previous session first and starts over.

        Returns True when discovery completed in time and False when
        `discovery_timeout` elapsed first. A False leaves the connection
        up and discovery continuing; await
        :meth:`wait_for_initial_discovery` rather than restarting.
        Raises ``AmpioNotConfigured`` as :meth:`wait_for_initial_discovery`
        does. Raises ``AmpioAuthError`` when the broker rejects the
        credentials and ``AmpioConnectionError`` when the session does not
        come up within `timeout` for any other reason.
        """
        await self._connection.open(timeout)
        try:
            await self._cancel_refresh_task()
            if self._refresh_interval is not None:
                self._refresh_task = asyncio.get_running_loop().create_task(
                    self._refresh_periodically(self._refresh_interval)
                )
            return await self.wait_for_initial_discovery(timeout=discovery_timeout)
        except asyncio.CancelledError:
            # The session is up and the refresh task can be running, and a
            # caller that abandons the setup holds neither. The shield keeps
            # a second cancel from leaving the teardown half-done.
            await asyncio.shield(self.disconnect())
            raise

    async def wait_for_initial_discovery(self, *, timeout: float = 8.0) -> bool:
        """Block until the initial discovery cycle has populated the client.

        Waits for the class's initial replies: the states snapshot, the
        server info, the object catalogue pair (data/devices ->
        ``objects``, data/params_devices -> the config columns), and, on
        :class:`AmpioAdminClient`, the module list (config/devices ->
        ``modules``).
        Returns True on completion and False if ``timeout`` elapses first.

        A True guarantees ``objects`` and ``server_info`` (and, on the
        admin client, ``modules``) are populated, with
        :pyattr:`AmpioServerInfo.server_key` a string by construction.
        Raises :class:`AmpioNotConfigured` on either installer fault: a
        catalogue row with no leaf, or two module rows on one override
        mac. The rows the door admitted are served, the connection stays
        up, and the pushed reply after the installer fixes Designer
        admits the refused rows, after which this returns True. It never
        raises on timeout - discovery continues and this returns False.
        Safe to call repeatedly and after reconnects. Each call re-checks
        the installer faults, so a later fault raises even after an
        earlier True.
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
        failure = self._store.admission_failure()
        if failure is not None:
            raise failure
        return True

    async def disconnect(self) -> None:
        """Close the connection.

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
        """Re-request the client's initial-discovery set.

        ``connect()`` issues this once on every (re)connect; call it to force
        a fresh discovery cycle without reconnecting. The call also resets
        the store's live-value protection, so the requested snapshot can
        correct a value that carries only a local receive stamp, which a raw
        channel edge leaves behind.
        """
        self._store.begin_refresh()
        for name in self._initial_endpoints:
            await self._publish(ENDPOINT_BY_NAME[name])

    async def fetch_rooms(self, timeout: float = 5.0) -> dict[int, str]:
        """Return ``{ampio_object_id: room_name}`` for objects assigned to a room.

        Objects assigned to multiple groups map to the first room
        encountered.

        Requires ``connect()`` to have completed. Raises ``AmpioConnectionError``
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

        Requires ``connect()`` to have completed. Raises ``AmpioConnectionError``
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

    async def set_event(self, event_number: int) -> None:
        """Raise a bus event, running whatever Ampio logic is bound to it.

        Works on both account tiers and is bounded by nothing - see the
        bus-events section of docs/bus-events.md for the rights model.
        ``event_number`` is 1-65535. Anything else, a bool included, raises
        ``AmpioValueError``.
        """
        _check_range("event_number", event_number, 1, 65535)
        await self._connection.publish(
            command_topic(self._username), event_payload(event_number).encode()
        )

    async def send_notification(self, message: str) -> None:
        """Push a notification to every user of the install's mobile app.

        Reaches the app on both account tiers. The M-SERV answers on no
        topic, so the call returns once the broker accepts the publish and
        it can never report delivery.

        Every registered user receives it.

        Raises ``AmpioValueError`` for an empty message, and for one that
        contains ``/``.
        """
        if not message:
            raise AmpioValueError("message must not be empty")
        if "/" in message:
            raise AmpioValueError(
                "message must not contain '/' - the M-SERV reads what follows "
                f"it as a user name and drops it, got {message!r}"
            )
        await self._connection.publish(
            command_topic(self._username), notification_payload(message).encode()
        )

    async def run_scene(self, scene_id: int) -> None:
        """Apply a scene's actions."""
        await self._scene_command(scene_id, "run")

    async def off_scene(self, scene_id: int) -> None:
        """Turn off the objects a scene drives."""
        await self._scene_command(scene_id, "off")

    async def undo_scene(self, scene_id: int) -> None:
        """Restore the objects a scene drives to the state they held before it ran."""
        await self._scene_command(scene_id, "undo")

    async def _scene_command(self, scene_id: int, verb: str) -> None:
        """Publish a scene command."""
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
        the verbs the library does not wrap - docs/commands.md carries the
        verb table, the grant-scoping rules, and what the M-SERV silently
        ignores.

        ``confirm`` opts into awaiting that state: the call returns the
        snapshot of the next :class:`~ampio_mqtt.events.ObjectUpdated`
        for the object within ``confirm`` seconds, raising
        ``AmpioTimeoutError`` on expiry. A concurrent change also
        satisfies it. See docs/commands.md.

        Raises ``AmpioValueError`` for an id the catalogue does not list.
        Raises ``AmpioConnectionError`` when the broker is unreachable and
        ``AmpioTimeoutError`` when it fails to acknowledge in time; never an
        aiomqtt exception type.
        """
        return await self._publish_command(
            command_topic(self._username),
            command_payload(object_id, verb, args).encode(),
            object_id,
            repr(verb),
            confirm,
        )

    async def _publish_command(
        self,
        topic: str,
        payload: bytes,
        object_id: int,
        action: str,
        confirm: float | None,
    ) -> AmpioObject | None:
        """Publish one object write, optionally awaiting the state echo.

        The confirm semantics documented on :meth:`command` live here;
        both the `/api` surface and the raw CAN write topic share them,
        since neither has a reply topic of its own.
        """
        if object_id not in self._store.objects:
            raise AmpioValueError(f"object {object_id} is not in the catalogue")
        if confirm is None:
            await self._connection.publish(topic, payload)
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
                await self._connection.publish(topic, payload)
                return await future
        except TimeoutError as err:
            raise AmpioTimeoutError(
                f"No state echo for object {object_id} within {confirm}s - "
                f"the M-SERV ignored {action}, the object is outside this "
                "account's grant, the object is read-only, or the command "
                "changed nothing"
            ) from err
        finally:
            unsubscribe()

    async def turn_on(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Turn an object fully on.

        Outputs and flags are both valid targets: a `flaga` object answers
        this verb family over `/api` on either account tier, which is what
        lets a consumer model a writable flag as a switch entity
        (:attr:`InputKind.switchable`). A `wej` is read-only.

        Raises ``AmpioUnsupported`` for an output whose kind says this verb
        does not apply. Turning an ``rgbw`` light on means choosing a
        color - the consumer's call, via :meth:`set_colors` (the rgbw
        replay pattern in docs/commands.md). A ``ledww`` light refuses for
        the matching reason: the power it had before is the consumer's to
        remember, and :meth:`set_ww_power` is what replays it. ``confirm``
        awaits the state echo exactly as :meth:`command` documents.
        """
        self._check_switchable(object_id, "turnOn")
        return await self.command(object_id, "turnOn", confirm=confirm)

    async def turn_off(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Turn an object off.

        Outputs and flags are both valid targets, exactly as
        :meth:`turn_on` documents.

        A color output that does not answer the switch verbs (``rgbw``) is
        turned off with ``setColors 0/0/0/0`` instead. A color-temperature
        output (``ledww``) is turned off with ``setWWPower 0``, which also
        holds its color temperature for the next turn-on. ``confirm``
        awaits the state echo exactly as :meth:`command` documents.
        """
        kind = self._output_kind(object_id)
        if kind is not None and not kind.switchable and kind.color:
            return await self.set_colors(object_id, 0, 0, 0, 0, confirm=confirm)
        if kind is not None and not kind.switchable and kind.color_temp:
            return await self.set_ww_power(object_id, 0, confirm=confirm)
        return await self.command(object_id, "turnOff", confirm=confirm)

    async def switch(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Invert an object's current on/off state.

        Outputs and flags are both valid targets, exactly as
        :meth:`turn_on` documents.

        Raises ``AmpioUnsupported`` for an output whose kind says the switch
        verbs do not apply (``rgbw``), exactly as :meth:`turn_on` does.
        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        self._check_switchable(object_id, "switch")
        return await self.command(object_id, "switch", confirm=confirm)

    def _output_kind(self, object_id: int) -> OutputKind | None:
        """The object's kind when it is a known output, else None."""
        obj = self._store.objects.get(object_id)
        kind = obj.kind if obj is not None else None
        return kind if isinstance(kind, OutputKind) else None

    def _value_range(self, object_id: int) -> tuple[int, int]:
        """The inclusive range `setValue` holds for one object."""
        obj = self._store.objects.get(object_id)
        kind = obj.kind if obj is not None else None
        if isinstance(kind, InputKind) and kind.value_range is not None:
            return kind.value_range
        return 0, 255

    def _check_switchable(self, object_id: int, verb: str) -> None:
        """Reject a switch-family verb for an output known not to answer it -
        the M-SERV would drop it with no effect and no reply. `switch` is
        gated separately from `turnOn`/`turnOff`, because a `ledww` answers
        it and ignores the other two."""
        kind = self._output_kind(object_id)
        if kind is None:
            return
        answers = kind.toggleable if verb == "switch" else kind.switchable
        if not answers:
            raise AmpioUnsupported(
                f"object {object_id} ({kind.key}) does not answer {verb}; "
                f"drive it with {'set_ww_power()' if kind.color_temp else 'set_colors()'}"
            )

    def _check_pulsable(self, object_id: int) -> None:
        """Reject a `pulse_ms` for a kind no timed write pulses. An analog
        flag is the case that bites: it takes the timed form, sets the
        value and never reverts, so the caller would get a permanent write
        where it asked for a press. `AmpioObject.pulse_ms` reads 0 for
        every kind this refuses."""
        obj = self._store.objects.get(object_id)
        kind = obj.kind if obj is not None else None
        if not isinstance(kind, InputKind | OutputKind) or kind.pulsable:
            return
        raise AmpioUnsupported(
            f"object {object_id} ({kind.key}) does not pulse; "
            f"the setValue time argument does not revert it"
        )

    async def set_value(
        self,
        object_id: int,
        value: int,
        *,
        pulse_ms: int | None = None,
        confirm: float | None = None,
    ) -> AmpioObject | None:
        """Set an object's level (relay, flag, dimmer).

        The range is 0-255 for everything but the analog flags, which
        carry their own width: a `flaga_liniowa16` reaches -32768 to
        32767. :pyattr:`InputKind.value_range` states it, and a value
        past it raises ``AmpioValueError``.

        With ``pulse_ms`` the M-SERV reverts the object to its previous state
        after that many milliseconds - a timed pulse, not a fade. The wire unit
        is 10 ms, so the value is rounded down to the nearest 10 ms; a gate
        pulse of 500 ms is ``pulse_ms=500``. ``confirm`` awaits the state
        echo as :meth:`command` documents - for a pulse that is the set
        edge, not the later revert.

        Raises ``AmpioUnsupported`` for an output whose level this verb
        cannot reach: ``rgbw`` (drive it with :meth:`set_colors`), ``ledww``,
        whose power axis moves through :meth:`set_ww_power` alone, and
        every cover, which moves through :meth:`open`, :meth:`close` and,
        with a position axis, :meth:`set_roller_pos`.

        ``pulse_ms`` reaches the relay, the flag and the dimmer alone,
        and it raises for every other established kind. The two analog
        flags are the ones that would surprise a caller: they take the
        timed form, set the value and latch, so a pulse would land as a
        permanent write. :pyattr:`AmpioObject.pulse_ms` reads 0 for every
        kind this refuses, so a consumer that honors that field never
        trips the check.

        Raises ``AmpioValueError`` for a value outside the range or a
        ``pulse_ms`` outside 0-655350.
        """
        _check_range("value", value, *self._value_range(object_id))
        kind = self._output_kind(object_id)
        if kind is not None and (kind.color or kind.color_temp or kind.cover):
            if kind.cover:
                replacement = "set_roller_pos()"
            elif kind.color_temp:
                replacement = "set_ww_power()"
            else:
                replacement = "set_colors()"
            raise AmpioUnsupported(
                f"object {object_id} ({kind.key}) does not answer setValue; "
                f"drive it with {replacement}"
            )
        if pulse_ms is None:
            return await self.command(object_id, "setValue", value, confirm=confirm)
        self._check_pulsable(object_id)
        _check_range("pulse_ms", pulse_ms, 0, 655350)
        return await self.command(
            object_id, "setValue", value, pulse_ms // 10, confirm=confirm
        )

    async def set_temperature(
        self, object_id: int, temperature: float, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Set a thermostat's (``reg``) target temperature in °C.

        The regulator echoes the new target in its state push, readable as
        :attr:`AmpioObject.thermostat`. Raises ``AmpioValueError`` for a bool,
        a non-number or a non-finite float. ``confirm`` awaits the state echo
        exactly as :meth:`command` documents.
        """
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
        ):
            raise AmpioValueError(
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
        ``AmpioValueError`` here rather than being dropped by the M-SERV
        (:meth:`command` is the escape hatch for experimenting).
        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        if mode not in HEATING_MODES:
            raise AmpioValueError(
                f"mode must be one of {sorted(HEATING_MODES)}, got {mode!r}"
            )
        return await self.command(object_id, "setHeatingMode", mode, confirm=confirm)

    async def set_colors(
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

        Raises ``AmpioValueError`` for an argument outside its range.
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

    async def set_ww(
        self,
        object_id: int,
        power: int,
        coldness: int,
        *,
        confirm: float | None = None,
    ) -> AmpioObject | None:
        """Set a CCT light's power and color-temperature axes, each 0-255.

        The object reports the two axes back as :attr:`AmpioObject.cct`.
        ``coldness`` is the raw byte the wire carries, not a temperature in
        kelvin. ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.

        Raises ``AmpioValueError`` for an argument outside its range.
        """
        for name, axis in (("power", power), ("coldness", coldness)):
            _check_range(name, axis, 0, 255)
        return await self.command(
            object_id, "setWW", power | coldness << 8, confirm=confirm
        )

    async def set_ww_power(
        self, object_id: int, power: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Set a CCT light's power axis alone, 0-255.

        The color temperature holds. ``confirm`` awaits the state echo exactly as
        :meth:`command` documents.

        Raises ``AmpioValueError`` for an argument outside its range.
        """
        _check_range("power", power, 0, 255)
        return await self.command(object_id, "setWWPower", power, confirm=confirm)

    async def set_ww_coldness(
        self, object_id: int, coldness: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Set a CCT light's color-temperature axis alone, 0-255.

        The power holds. ``coldness`` is the raw byte the wire carries, not a
        temperature in kelvin. ``confirm`` awaits the state echo exactly as
        :meth:`command` documents.

        Raises ``AmpioValueError`` for an argument outside its range.
        """
        _check_range("coldness", coldness, 0, 255)
        return await self.command(object_id, "setWWColdness", coldness, confirm=confirm)

    async def open(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Drive a cover to fully open (position 100).

        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        return await self.command(object_id, "open", confirm=confirm)

    async def close(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Drive a cover to fully closed (position 0).

        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.
        """
        return await self.command(object_id, "close", confirm=confirm)

    async def stop(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Halt a cover wherever it is, on either axis - a stationary cover
        is a silent no-op. The `stop` row of docs/commands.md details the
        mid-travel and mid-rotation behavior. ``confirm`` awaits the state
        echo exactly as :meth:`command` documents."""
        return await self.command(object_id, "stop", confirm=confirm)

    async def set_roller_pos(
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
        slat-drag note in docs/commands.md). Position updates stream in as
        the cover travels; ``confirm`` awaits the first of them exactly as
        :meth:`command` documents, so its snapshot reads the travel's start,
        not its end.

        A blocked direction drops this command in silence (docs/commands.md).

        Raises ``AmpioValueError`` for an argument outside its range.
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

    async def set_roller_lamella(
        self, object_id: int, lamella: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Set a blind's slat angle percent, leaving its position alone.

        ``confirm`` awaits the state echo exactly as :meth:`command`
        documents.

        A blocked direction drops this command in silence (docs/commands.md).

        Raises ``AmpioValueError`` for an argument outside its range.
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
                raise RuntimeError(f"endpoint {name!r} is not served to this client")
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


class AmpioAdminClient(AmpioClient):
    """The client for the reserved ``admin`` login.

    Everything :class:`AmpioClient` serves, plus what the M-SERV serves
    that login alone: the module catalogue, the raw tree with its
    low-latency bridge and the module diagnostics, the description-record
    sweep, and the CAN write frames. The class carries its username, so no
    caller passes one. docs/account-tiers.md lists the members.
    """

    _endpoints = ADMIN_ENDPOINTS
    _store_class = AdminStore
    _routes_admin_shapes = True

    # The store `_store_class` builds, narrowed so the admin members read
    # the module catalogue the base store does not hold.
    _store: AdminStore

    def __init__(
        self,
        host: str,
        password: str | None = None,
        *,
        port: int = 1883,
        reconnect_interval: float = 5.0,
        refresh_interval: float | None = None,
        mqtt_client_factory: _connection.MqttClientFactory | None = None,
    ) -> None:
        # Futures awaiting the next device_api list reply; every waiter
        # receives the same reply, exactly as endpoint fetches share one.
        self._device_list_waiters: list[
            asyncio.Future[tuple[_protocol.DeviceRecord, ...]]
        ] = []
        # Last digest per app-sync table from the retained `md5` topics;
        # the first value per table seeds. The tasks that answer a change
        # with the module-list request are held so a pending one is never
        # garbage-collected mid-publish.
        self._digests: dict[str, str] = {}
        self._module_list_tasks: set[asyncio.Task[None]] = set()
        # What the last resolve_records() pass covered. None until one runs.
        self._last_sweep: RecordSweep | None = None
        self._module_list_endpoint = ENDPOINT_BY_NAME["devices"]
        super().__init__(
            host,
            AccessTier.ADMIN.value,
            password,
            port=port,
            reconnect_interval=reconnect_interval,
            refresh_interval=refresh_interval,
            mqtt_client_factory=mqtt_client_factory,
        )

    def _subscriptions(self) -> list[tuple[str, int]]:
        """The base filter set plus the shapes the admin login alone is served.

        The M-SERV never pushes the module list. The retained digests of
        the app-sync tables reveal a Designer save to the admin session,
        which then re-requests it (#166).
        """
        digests = [
            _protocol.md5_topic(self._username, keyword)
            for keyword in _protocol.CATALOGUE_DIGEST_KEYWORDS
        ]
        # These filters keep the acknowledged leg. Their retained replay is
        # at most one diagnostics frame per module, far below the broker's
        # QoS 1 queue.
        raw_live = [
            RAW_DIAGNOSTICS_WILDCARD,
            RAW_EVENT_WILDCARD,
            _protocol.DEVICE_API_LIST_TOPIC,
        ]
        # The retained raw state tree rides QoS 0 and everything else QoS 1;
        # the two constants in ``_connection`` carry the reasoning.
        raw_state = [
            *RAW_INPUT_WILDCARDS,
            RAW_OUTPUT_WILDCARD,
            RAW_ANALOG_WILDCARD,
            *RAW_COLOR_TEMP_WILDCARDS,
        ]
        return [
            *super()._subscriptions(),
            *((t, _connection.SUBSCRIBE_QOS) for t in (*digests, *raw_live)),
            *((t, _connection.RAW_STATE_QOS) for t in raw_state),
        ]

    def _apply_inbound(self, msg: _protocol.Inbound, retained: bool) -> Applied:
        """Peel the device list and the catalogue digests off, then hand
        every other message to the base client."""
        if isinstance(msg, _protocol.DeviceList):
            waiters, self._device_list_waiters = self._device_list_waiters, []
            for future in waiters:
                if not future.done():
                    future.set_result(msg.devices)
            return Applied()
        if isinstance(msg, _protocol.CatalogueDigest):
            self._note_digest(msg)
            return Applied()
        return super()._apply_inbound(msg, retained)

    async def _handle_connected(self) -> None:
        """Seed the digests anew, then run the base on-connect step.

        The broker replays every retained digest after the subscribe, and
        the refresh fetches the catalogues anyway, so the replay must seed
        rather than count as a change against the previous session's value.
        """
        self._digests.clear()
        await super()._handle_connected()

    def _note_digest(self, digest: _protocol.CatalogueDigest) -> None:
        """Re-request the module list when a pushed table digest changes."""
        previous = self._digests.get(digest.keyword)
        self._digests[digest.keyword] = digest.digest
        if previous is None or previous == digest.digest:
            return
        task = asyncio.get_running_loop().create_task(self._request_module_list())
        self._module_list_tasks.add(task)
        task.add_done_callback(self._module_list_tasks.discard)

    async def _request_module_list(self) -> None:
        """Publish the module-list request from a digest-change task.

        A publish failure ends this request only: the task runs outside
        the connection loop, which handles the drop itself, and the next
        (re)connect refreshes the module list anyway.
        """
        try:
            await self._publish(self._module_list_endpoint)
        except AmpioConnectionError as err:
            _LOGGER.debug("Ampio module-list re-request did not go out: %s", err)

    async def _cancel_module_list_tasks(self) -> None:
        tasks = list(self._module_list_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def disconnect(self) -> None:
        """Cancel the refresh task, then the module-list tasks, then close
        through the base client."""
        # The refresh task goes first: a module-list cancellation awaited
        # while a refresh is ready lets that refresh publish once more.
        await self._cancel_refresh_task()
        await self._cancel_module_list_tasks()
        await super().disconnect()

    @property
    def modules(self) -> Mapping[int, AmpioModule]:
        """All known physical modules keyed by id, as a read-only live view.

        The M-SERV serves the module catalogue to the reserved ``admin``
        login alone. A consumer that must group entities by module on any
        account reads ``AmpioObject.address.mac``, which the catalogue
        carries for every account (docs/identity.md).
        """
        return MappingProxyType(self._store.modules)

    @property
    def mserv(self) -> AmpioModule | None:
        """The M-SERV's own module row, for naming the hub device.

        The row whose ``mac_global`` or ``mac`` is the server's
        self-reported mac.

        None until both the module catalogue and the server info have
        arrived, which :meth:`wait_for_initial_discovery` waits for. A
        lasting None says no admitted module row is the M-SERV's own
        (absent, or refused for a mac collision). Device grouping that needs
        no module row at all is :pyattr:`AmpioObject.is_server_owned`.
        """
        info = self._store.server_info
        if info is None:
            return None
        for mod in self._store.modules.values():
            if info.mac in (mod.mac_global, mod.mac):
                return mod
        return None

    def module_for(self, obj: AmpioObject) -> AmpioModule | None:
        """The catalogue row of the module that owns ``obj``.

        One lookup on ``obj.address.mac``, the override mac the leaf embeds
        and the module list's replacement-stable key (docs/identity.md).
        None when the list carries no row on that mac, or when two rows
        share it and no lookup can pick one. Grouping that needs no module
        catalogue reads ``address.mac`` directly.
        """
        return self._store.module_by_mac(obj.address.mac)

    @property
    def records(self) -> Mapping[int, DesignerRecord]:
        """Each object's description-record entry, by object id.

        Present: the module answered a sweep and carries the entry. Absent
        with the object's ``address.mac`` in ``last_sweep.answered_macs``:
        the module answered and carries no entry. That answer covers the
        objects the catalogue lists when the sweep runs, so an object the
        catalogue admits later needs another sweep before absence answers
        for it. Absent with the mac not answered: not known.
        docs/description-records.md.
        """
        return MappingProxyType(self._store.records)

    @property
    def cover_parameters(self) -> Mapping[int, CoverParameters]:
        """Each cover's stored travel parameters, by object id, under the
        rule :pyattr:`records` states. Absence can also mean this library
        has not proven that board's roller layout. An object the catalogue
        moves out of the roller class drops its entry."""
        return MappingProxyType(self._store.cover_parameters)

    @property
    def module_records(self) -> Mapping[int, ModuleRecord]:
        """Each module's DEVICE_NAME entry, by mac, under the rule
        :pyattr:`records` states."""
        return MappingProxyType(self._store.module_records)

    @property
    def capabilities(self) -> Mapping[int, Mapping[int, int]]:
        """Each module's ``{function id: channel count}`` map, by mac.

        Every module that answered a sweep has an entry, an empty map when
        it advertises nothing. Index a map with :class:`ModuleFunction`; an
        id without a member reads through under its number.
        """
        return MappingProxyType(self._store.capabilities)

    @property
    def panel_settings(self) -> Mapping[int, PanelSettings]:
        """Each touch panel's stored settings, by mac, under the rule
        :pyattr:`records` states. Absence can also mean this library has not
        proven that panel's layout."""
        return MappingProxyType(self._store.panel_settings)

    @property
    def last_sweep(self) -> RecordSweep | None:
        """What the last :meth:`resolve_records` pass covered, or None
        before the first."""
        return self._last_sweep

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """The base report plus what the module catalogue adds.

        Two keys on top of :meth:`AmpioClient.diagnostics_snapshot`:

        - ``mac_collisions``: the door's record of every override mac two
          or more module rows share, as ``[mac, [module ids]]``.
        - ``modules``: one row per known module, sorted by id, with the
          :class:`AmpioModule` fields ``id``, ``mac``, ``typ_urzadzenia``,
          ``model``, ``last_seen``, ``supply_voltage``, and
          ``temperature``. The user-given module name stays out.

        Both entries write the mac through :func:`format_mac`, as the
        string ``"0xCB8F"``. :attr:`AmpioModule.mac` keeps the integer.
        The mac in ``server_info`` stays an integer.
        """
        snapshot = super().diagnostics_snapshot()
        snapshot["mac_collisions"] = [
            [format_mac(mac), list(ids)] for mac, ids in self._store.collisions
        ]
        snapshot["modules"] = [
            {
                "id": module.id,
                "mac": format_mac(module.mac),
                "typ_urzadzenia": module.typ_urzadzenia,
                "model": module.model,
                "last_seen": module.last_seen,
                "supply_voltage": module.supply_voltage,
                "temperature": module.temperature,
            }
            for module in sorted(self._store.modules.values(), key=lambda m: m.id)
        ]
        return snapshot

    async def fetch_locations(self, timeout: float = 5.0) -> dict[int, str]:
        """Return ``{location_id: name}`` - the Designer "Lokalizacja" table.

        The name table the per-output location pointer resolves through;
        :meth:`resolve_records` consumes it and per-object consumers read
        :pyattr:`records` instead.

        Requires ``connect()`` to have completed. Raises
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

    async def resolve_records(self, timeout: float = 10.0) -> RecordSweep:
        """Read every module's CAN description record and return what the pass covered.

        The call replaces the five datasets (:pyattr:`records`,
        :pyattr:`cover_parameters`, :pyattr:`module_records`,
        :pyattr:`capabilities`, :pyattr:`panel_settings`) for every mac
        the reply answered, sets :pyattr:`last_sweep`, and dispatches one
        :class:`RecordSweepCompleted`. A sweep changes no model field, so
        it dispatches no :class:`ObjectUpdated` or :class:`ModuleUpdated`.

        Returns a :class:`RecordSweep`. Its ``records`` map is
        ``{object_id: DesignerRecord}`` for what this pass resolved, and
        its two mac sets say which modules the reply listed and which
        catalogued modules it left out. A module the reply left out keeps
        every dataset entry an earlier pass gave it.

        ``timeout`` bounds each of the two replies, the name table and
        the list, so the call ends within twice that.

        Requires ``connect()`` to have completed. Raises
        ``AmpioConnectionError`` if the broker is not connected and
        ``AmpioTimeoutError`` if either reply does not arrive.
        """
        names = await self.fetch_locations(timeout=timeout)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[_protocol.DeviceRecord, ...]] = (
            loop.create_future()
        )
        self._device_list_waiters.append(future)
        try:
            async with asyncio.timeout(timeout):
                await self._connection.publish(
                    _protocol.DEVICE_API_LIST_REQUEST,
                    _protocol.DEVICE_API_LIST_PAYLOAD,
                )
                devices = await future
        except TimeoutError as err:
            raise AmpioTimeoutError(
                "Timed out fetching the module records from the Ampio broker"
            ) from err
        finally:
            if future in self._device_list_waiters:
                self._device_list_waiters.remove(future)
        # Keyed by the override mac the reply carries: the id every leaf
        # embeds, so the join needs no catalogue lookup.
        by_mac = {device.mac: device.entries for device in devices}
        catalogued = {mod.mac for mod in self._store.modules.values()}
        silent = frozenset(catalogued - by_mac.keys())
        if silent:
            _LOGGER.warning(
                "Ampio modules %s are missing from the device list; their "
                "objects keep whatever record an earlier pass resolved",
                ", ".join(format_mac(mac) for mac in sorted(silent)),
            )
        resolved = _protocol.resolve_designer(self._store.objects, by_mac, names)
        params_by_mac = {device.mac: device.params for device in devices}
        hardware_by_mac = {
            mod.mac: (mod.typ_urzadzenia, mod.wersja_pcb)
            for mod in self._store.modules.values()
        }
        capabilities = _protocol.resolve_module_capabilities(
            {device.mac: device.capabilities for device in devices}
        )
        self._store.apply_sweep(
            frozenset(by_mac),
            resolved,
            _protocol.resolve_cover_parameters(
                self._store.objects,
                params_by_mac,
                capabilities,
                hardware_by_mac,
            ),
            _protocol.resolve_module_records(by_mac, names),
            capabilities,
            _protocol.resolve_panel_settings(
                params_by_mac, capabilities, hardware_by_mac
            ),
        )
        sweep = RecordSweep(
            records=dict(resolved),
            answered_macs=frozenset(by_mac),
            silent_macs=silent,
        )
        self._last_sweep = sweep
        self._dispatch(RecordSweepCompleted(sweep))
        return sweep

    def _raw_output_address(self, object_id: int) -> tuple[int, int, int] | None:
        """The (module mac, frame channel, function byte) of an output the
        admin session drives over the raw CAN write topic, or None.

        `przekaznik` objects on CAN modules, addressed by their own leaf:
        mac from ``address.mac`` (the replacement-stable override), the
        0-based ``address.channel``, and the function byte the leaf class
        takes (:data:`RAW_OUTPUT_FUNCTION_BY_SF`). A class outside that
        table stays on `/api`. The M-SERV's own virtual outputs stay on
        `/api` too - they live in its DB, not on the CAN bus.
        """
        obj = self._store.objects.get(object_id)
        if obj is None or obj.typ_komponentu != "przekaznik" or obj.is_server_owned:
            return None
        function = RAW_OUTPUT_FUNCTION_BY_SF.get(obj.address.sf_id)
        if function is None:
            return None
        return obj.address.mac, obj.address.channel, function

    async def _raw_output(
        self,
        object_id: int,
        address: tuple[int, int, int],
        value: int,
        confirm: float | None,
    ) -> AmpioObject | None:
        """Drive an output over the raw CAN write topic."""
        mac, channel, function = address
        return await self._publish_command(
            raw_write_topic(mac),
            raw_output_payload(function, value, channel).encode(),
            object_id,
            "the raw output write",
            confirm,
        )

    async def turn_on(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Turn an object fully on.

        A binary or open-collector output on a CAN module rides the raw CAN
        write topic, the one write that reaches a panel's status LEDs, which ignore
        `/api` for every account (docs/panel-writes.md, "Panel outputs").
        A flag never takes that path: the raw frame addresses a module's
        output channels, which a flag index does not index. Every other
        object takes the base path.
        """
        address = self._raw_output_address(object_id)
        if address is None:
            return await super().turn_on(object_id, confirm=confirm)
        # A `przekaznik` always classifies switchable and toggleable, so no
        # switch-verb check can ever raise on this arm.
        return await self._raw_output(object_id, address, 255, confirm)

    async def turn_off(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Turn an object off.

        A binary or open-collector output rides the raw frame as
        :meth:`turn_on` does.
        """
        address = self._raw_output_address(object_id)
        if address is None:
            return await super().turn_off(object_id, confirm=confirm)
        return await self._raw_output(object_id, address, 0, confirm)

    async def switch(
        self, object_id: int, *, confirm: float | None = None
    ) -> AmpioObject | None:
        """Invert an object; the raw frame has no invert, so the held value decides.

        A binary or open-collector output rides the raw frame as
        :meth:`turn_on` does, and
        an object with no value yet reads off and turns on.
        """
        address = self._raw_output_address(object_id)
        if address is None:
            return await super().switch(object_id, confirm=confirm)
        obj = self._store.objects[object_id]
        return await self._raw_output(
            object_id, address, 0 if obj.is_on else 255, confirm
        )

    async def set_value(
        self,
        object_id: int,
        value: int,
        *,
        pulse_ms: int | None = None,
        confirm: float | None = None,
    ) -> AmpioObject | None:
        """Set a level.

        An untimed write to a binary or open-collector output rides the raw
        frame.

        A pulse always rides `/api` - the raw write frame has no timed
        form - so a panel output, which ignores `/api`, cannot pulse;
        ``confirm`` is what surfaces that (docs/panel-writes.md, "Panel
        outputs").
        """
        if pulse_ms is not None:
            return await super().set_value(
                object_id, value, pulse_ms=pulse_ms, confirm=confirm
            )
        address = self._raw_output_address(object_id)
        if address is None:
            return await super().set_value(object_id, value, confirm=confirm)
        _check_range("value", value, *self._value_range(object_id))
        return await self._raw_output(object_id, address, value, confirm)

    # --- module writes (the raw CAN write path) --------------------------

    def _raw_write_mac(self, module_id: int) -> int:
        """The bus address a raw frame addressed to one module goes to.

        Dumb routing by module, as the raw output frame is by leaf: any
        catalogued module is a valid address. The buzzer frames are proven
        on M-DOT panels, the identify frames on panels and DIN-rail
        modules.
        """
        module = self._store.modules.get(module_id)
        if module is None:
            raise AmpioValueError(f"module id {module_id} is not in the catalogue")
        return module.mac

    @staticmethod
    def _buzz_ticks(name: str, seconds: float, limit: float) -> int:
        if not 0 <= seconds <= limit:
            raise AmpioValueError(
                f"{name} must be within 0 and {limit} s, got {seconds}"
            )
        return round(seconds * 100)

    async def buzz(
        self, module_id: int, *, tone: int = 6, seconds: float = 0.5
    ) -> None:
        """Sound a panel's buzzer once.

        ``module_id`` is :pyattr:`AmpioModule.id`. ``tone`` 1-31 sets the
        pitch, and 6 is the loudest. ``seconds`` 0.01-2.55 in
        10 ms steps; 0 is refused, since a zero time latches the buzzer
        on. Use :meth:`buzz_pattern` with ``cycles=0`` for a sound that
        lasts until :meth:`buzz_stop`.

        ``AmpioValueError`` for an argument outside its range or an
        unknown module, both before any publish. No readback exists - the
        panel confirms nothing on the bus - so there is no ``confirm``.
        docs/panel-writes.md ("Panel buzzer") carries the frame and the
        tone table.
        """
        mac = self._raw_write_mac(module_id)
        ticks = self._buzz_ticks("seconds", seconds, 2.55)
        if ticks == 0:
            raise AmpioValueError(
                "seconds must be at least 0.01 - a zero time latches the buzzer on"
            )
        _check_range("tone", tone, 1, 31)
        payload = raw_buzzer_payload(True, tone, ticks)
        await self._connection.publish(raw_write_topic(mac), payload.encode())

    async def buzz_pattern(
        self,
        module_id: int,
        *,
        tone: int,
        seconds: float,
        tone2: int = 0,
        seconds2: float = 0.0,
        cycles: int = 1,
        delay: float = 0.0,
    ) -> None:
        """Play a two-tone pattern on a panel's buzzer.

        Each cycle sounds ``tone`` for ``seconds``, then ``tone2`` for
        ``seconds2``; tone 0 is a silent rest, so three short pips are
        ``tone=6, seconds=0.3, seconds2=0.3, cycles=3``. ``cycles`` 0
        repeats until :meth:`buzz_stop` or another pattern. ``delay``
        postpones the start. Times take 0-655.35 s in 10 ms steps, tones
        0-31, cycles 0-254. The same rules as :meth:`buzz` apply to the
        errors and the missing readback.
        """
        mac = self._raw_write_mac(module_id)
        _check_range("tone", tone, 0, 31)
        _check_range("tone2", tone2, 0, 31)
        _check_range("cycles", cycles, 0, 254)
        payload = raw_buzzer_pattern_payload(
            tone,
            self._buzz_ticks("seconds", seconds, 655.35),
            tone2,
            self._buzz_ticks("seconds2", seconds2, 655.35),
            cycles,
            self._buzz_ticks("delay", delay, 655.35),
        )
        await self._connection.publish(raw_write_topic(mac), payload.encode())

    async def buzz_stop(self, module_id: int) -> None:
        """Silence a panel's buzzer.

        Ends a running pattern and a plain beep alike. The same rules as
        :meth:`buzz` apply to the errors.
        """
        topic = raw_write_topic(self._raw_write_mac(module_id))
        await self._connection.publish(topic, RAW_BUZZER_SILENCE.encode())
        await self._connection.publish(topic, RAW_BUZZER_OFF.encode())

    async def identify(self, module_id: int) -> None:
        """Light a module's CAN LED steadily so it can be found by eye.

        The Designer's "Identify device" button. ``module_id`` is
        :pyattr:`AmpioModule.id`. The LED stays on until
        :meth:`identify_stop`, and the library schedules no stop.

        ``AmpioValueError`` for an unknown module, before any publish. No
        readback exists - the module confirms nothing on the bus - so
        there is no ``confirm``.
        docs/panel-writes.md ("Module identify") carries the frame.
        """
        topic = raw_write_topic(self._raw_write_mac(module_id))
        await self._connection.publish(topic, RAW_IDENTIFY_ON.encode())

    async def identify_stop(self, module_id: int) -> None:
        """Return a module's CAN LED to its normal blink after :meth:`identify`.

        The same rules as :meth:`identify` apply to the errors.
        """
        topic = raw_write_topic(self._raw_write_mac(module_id))
        await self._connection.publish(topic, RAW_IDENTIFY_OFF.encode())

    @staticmethod
    def _panel_mask(fields: Sequence[int] | None) -> str:
        """The touch field mask for one panel action.

        Always the full width (docs/panel-writes.md). A field number the frame
        cannot carry is refused; a field the panel does not have is ignored by
        the panel.
        """
        if fields is not None:
            for number in fields:
                _check_range("field", number, 1, MAX_PANEL_FIELD)
        return panel_field_mask(fields, PANEL_MASK_MAX_BYTES)

    async def set_panel_backlight(
        self,
        module_id: int,
        red: int,
        green: int,
        blue: int,
        white: int = 0,
        *,
        fields: Sequence[int] | None = None,
    ) -> None:
        """Set the resting color of a panel's touch field icons.

        ``module_id`` is :pyattr:`AmpioModule.id`. ``fields`` names the
        1-based touch fields to color, and None colors every field the
        panel has. Each channel is 0-255; the white channel drives the
        panel's own white LEDs, so ``0, 0, 0, 255`` is the plain white
        most installs configure.

        This is a runtime override, not a setting. It takes effect at
        once, writes no configuration, and a panel restart restores the
        module's :pyattr:`panel_settings` entry, its stored default.
        Nothing on the bus reports the current color, so there is no
        readback.

        ``AmpioValueError`` for an out-of-range value, including a field
        above :data:`MAX_PANEL_FIELD`, or an unknown module, both before
        any publish. docs/panel-writes.md carries the frame.
        """
        mac = self._raw_write_mac(module_id)
        for name, value in (
            ("red", red),
            ("green", green),
            ("blue", blue),
            ("white", white),
        ):
            _check_range(name, value, 0, 255)
        mask = self._panel_mask(fields)
        payload = raw_backlight_payload(red, green, blue, white, mask)
        await self._connection.publish(raw_write_topic(mac), payload.encode())

    async def set_panel_status_light(
        self,
        module_id: int,
        red: int,
        green: int,
        blue: int,
        *,
        fields: Sequence[int] | None = None,
    ) -> None:
        """Set the color a panel's status indicators show.

        The indicator is what reacts when a field is touched or its
        object is on. It has no white channel, which is the only
        difference from :meth:`set_panel_backlight`. The same rules apply
        to ``fields``, the errors, and the absent readback.
        """
        mac = self._raw_write_mac(module_id)
        for name, value in (("red", red), ("green", green), ("blue", blue)):
            _check_range(name, value, 0, 255)
        mask = self._panel_mask(fields)
        payload = raw_status_light_payload(red, green, blue, mask)
        await self._connection.publish(raw_write_topic(mac), payload.encode())

    async def lock_panel(self, module_id: int, *, seconds: float) -> None:
        """Ignore every touch on a panel for ``seconds``.

        ``module_id`` is :pyattr:`AmpioModule.id`. ``seconds`` is
        0.01-655.35 in 10 ms steps.

        The lock always expires. There is no indefinite form - a zero
        time is a lock of zero length, not a latch, so it is refused.
        Hold a panel locked by re-arming before the current lock runs
        out, and release it early with :meth:`unlock_panel`.

        No readback exists.

        ``AmpioValueError`` for an out-of-range time or an unknown module,
        both before any publish.
        """
        mac = self._raw_write_mac(module_id)
        ticks = self._buzz_ticks("seconds", seconds, 655.35)
        if ticks == 0:
            raise AmpioValueError(
                "seconds must be at least 0.01 - a zero time locks for no time"
            )
        payload = raw_key_lock_payload(True, ticks)
        await self._connection.publish(raw_write_topic(mac), payload.encode())

    async def unlock_panel(self, module_id: int) -> None:
        """Release a panel's touch lock at once.

        Ends a lock started by :meth:`lock_panel` or by the panel's own
        touch combination, without waiting for it to expire. The same
        rules as :meth:`lock_panel` apply to the errors.
        """
        mac = self._raw_write_mac(module_id)
        payload = raw_key_lock_payload(False, 0)
        await self._connection.publish(raw_write_topic(mac), payload.encode())

    def lock_target(self, object_id: int) -> LockTarget | LockRefusal:
        """The lock frame's target for one cover, or why none can go out.

        Reads the catalogue row and the module's entry in
        :pyattr:`capabilities` (docs/panel-writes.md). An object outside the
        roller description class (``roleta_procenty``, ``roleta_lamelki``) is
        refused, the plain ``roleta`` included. A non-cover's channel index can
        belong to a cover on the same module. The four lock methods call this
        and raise on a refusal, so a consumer that builds a lock control calls
        it after the sweep and leaves the control out on a refusal. Raises
        ``AmpioValueError`` for an id the catalogue does not list, and
        ``AmpioNotConfigured`` for a mac no admitted module row carries.
        """
        obj = self._store.objects.get(object_id)
        if obj is None:
            raise AmpioValueError(f"object {object_id} is not in the catalogue")
        if not joins_roller_records(obj.typ_komponentu):
            return LockRefusal.NOT_A_COVER
        mac, channel = obj.address.mac, obj.address.channel
        if self._store.module_by_mac(mac) is None:
            # The frame's whole destination is the mac. With no row on it
            # the install cannot say which module the frame would reach,
            # and the sweep can still hold what the mac answered earlier.
            shared = dict(self._store.collisions)
            raise AmpioNotConfigured(collisions=((mac, shared.get(mac, ())),))
        capabilities = self._store.capabilities.get(mac)
        if capabilities is None:
            return LockRefusal.NOT_SWEPT
        count = capabilities.get(ModuleFunction.ROLLER)
        if count is None:
            return LockRefusal.NO_ROLLER_COUNT
        if channel >= count:
            return LockRefusal.PAST_LAST_CHANNEL
        return LockTarget(mac=mac, channel=channel, channels=count)

    async def _roller_lock(
        self, object_id: int, sub_function: int, *, assert_lock: bool
    ) -> None:
        """Publish one roller lock frame for an object's own channel, or raise."""
        target = self.lock_target(object_id)
        if target is LockRefusal.NOT_SWEPT:
            raise AmpioValueError(
                f"no sweep has answered the module behind object {object_id}; "
                "call resolve_records() first"
            )
        if isinstance(target, LockRefusal):
            raise AmpioUnsupported(f"object {object_id}: {_LOCK_REFUSALS[target]}")
        await self._connection.publish(
            raw_write_topic(target.mac),
            raw_roller_lock_payload(
                sub_function, target.channel, target.channels, assert_lock=assert_lock
            ).encode(),
        )

    async def block_opening(self, object_id: int) -> None:
        """Stop a cover from opening until something releases it.

        The module then drops every opening command for that cover, the
        `/api` verbs included, with no error and no reply. A slat turn
        toward open counts as opening and is dropped on the same bit.
        The closing direction keeps working.

        The lock never expires. A consumer that sets one owns releasing
        it. :pyattr:`AmpioObject.blocks_opening` reads it back.

        Needs :meth:`resolve_records` to have run, because the module's
        roller channel count both gates the write and sizes the frame's
        channel mask. Calls :meth:`lock_target` and raises on a refusal:
        ``AmpioValueError`` when no sweep has answered the module yet,
        ``AmpioUnsupported`` for the other refusals, an object outside the
        roller description class, a module that advertises no roller
        count, or a count that does not reach this channel, and
        ``AmpioNotConfigured`` for a mac no admitted module row carries.
        Raises ``AmpioValueError`` for an id the catalogue does not list.
        """
        await self._roller_lock(object_id, ROLLER_BLOCK_OPENING, assert_lock=True)

    async def unblock_opening(self, object_id: int) -> None:
        """Let a cover open again, leaving any closing lock in place.

        The same rules as :meth:`block_opening` apply to the errors.
        """
        await self._roller_lock(object_id, ROLLER_BLOCK_OPENING, assert_lock=False)

    async def block_closing(self, object_id: int) -> None:
        """Stop a cover from closing until something releases it.

        The opening direction keeps working. Everything :meth:`block_opening`
        documents applies to this direction.
        """
        await self._roller_lock(object_id, ROLLER_BLOCK_CLOSING, assert_lock=True)

    async def unblock_closing(self, object_id: int) -> None:
        """Let a cover close again, leaving any opening lock in place.

        The same rules as :meth:`block_opening` apply to the errors.
        """
        await self._roller_lock(object_id, ROLLER_BLOCK_CLOSING, assert_lock=False)


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
        raise AmpioValueError(f"{name} must be an int in {low}..{high}, got {value!r}")
