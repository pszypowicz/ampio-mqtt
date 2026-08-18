"""The broker connection: one MQTT session, kept up.

Owns the aiomqtt client, the subscribe set, and the reconnect loop, and knows
nothing about what the messages mean - it hands each one to a callback. Keeping
that boundary means a protocol change never touches reconnect behaviour, and a
transport change never touches state.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress

import aiomqtt

from .errors import AmpioAuthError, AmpioConnectionError, AmpioTimeoutError
from .models import ConnectionStats

_LOGGER = logging.getLogger(__name__)

_RECONNECT_BACKOFF_MAX = 60.0
_AUTH_REJECTED = "Authentication rejected by Ampio broker"
# The M-SERV publishes everything at QoS 1, and per-object state topics are
# not retained, so a push lost in transit is gone until the next change.
# Subscribing at QoS 1 keeps the broker's at-least-once delivery leg; the
# default QoS 0 would downgrade it to at-most-once (#65).
_SUBSCRIBE_QOS = 1
# Publishes go out at QoS 1 too: the awaited publish then completes on the
# broker's PUBACK, so a returned command means "the broker accepted it"
# rather than "the payload left the socket" (#68). Every publish rides the
# same path, so commands, scene/event publishes, and discovery requests all
# get the acknowledged leg.
_PUBLISH_QOS = 1
# The PUBACK deadline for a QoS 1 publish. Owned here rather than left to
# aiomqtt's per-client operation timeout: aiomqtt reports its own timeout as
# a bare MqttError distinguishable only by message text, while a local
# deadline maps the case structurally to the retryable AmpioTimeoutError.
_PUBLISH_TIMEOUT = 5.0
# MQTT 5 reason codes for a credential rejection: 134 "bad user name or
# password", 135 "not authorized". paho's VERSION2 callbacks (aiomqtt >= 2.2,
# hence the pyproject floor) normalize CONNACK rejections to these codes
# whatever protocol version is on the wire, so they are the only auth shapes.
# A tuple, not a set: paho's ReasonCode compares equal to its integer value
# but is unhashable.
_AUTH_REASON_CODES = (134, 135)

MessageHandler = Callable[[str, str], None]
AvailabilityHandler = Callable[[bool], None]
ConnectedHandler = Callable[[], Awaitable[None]]
AuthFailureHandler = Callable[[str], None]
FatalHandler = Callable[[str], None]
# The transport seam: a zero-argument callable returning the session object
# for one connect attempt. The default builds the real aiomqtt.Client; tests
# inject a fake instance and skip both patching and class-level state.
MqttClientFactory = Callable[[], aiomqtt.Client]


class Connection:
    """Maintains one MQTT session to the broker, reconnecting as needed."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        *,
        reconnect_interval: float,
        topics: Sequence[str],
        stats: ConnectionStats,
        on_message: MessageHandler,
        on_availability: AvailabilityHandler,
        on_connected: ConnectedHandler,
        on_auth_failure: AuthFailureHandler,
        on_fatal: FatalHandler,
        client_factory: MqttClientFactory | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._reconnect_interval = reconnect_interval
        self._topics = tuple(topics)
        self._stats = stats
        self._on_message = on_message
        self._on_availability = on_availability
        self._on_connected = on_connected
        self._on_auth_failure = on_auth_failure
        self._on_fatal = on_fatal

        # Reusing one client id across reconnects keeps the broker from seeing
        # parallel "ghost" sessions while the previous one expires.
        self._client_id = f"ampio_mqtt_{uuid.uuid4().hex}"
        self._client_factory = client_factory or self._default_client
        self._client: aiomqtt.Client | None = None
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._auth_failed = asyncio.Event()
        self._auth_error_message: str | None = None
        self._died = asyncio.Event()
        self._fatal_message: str | None = None
        self._available = False
        self._stop = False
        # Set by close() before it cancels the runner, so the teardown's
        # availability drop is recognizably consumer-initiated however the
        # cancellation surfaces through aiomqtt's cleanup.
        self._closing = False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def auth_failure(self) -> str | None:
        """The broker's rejection reason once the loop has stopped for auth."""
        if not self._auth_failed.is_set():
            return None
        return self._auth_error_message or _AUTH_REJECTED

    async def open(self, timeout: float) -> None:
        """Start the loop and wait for the first connection.

        Raises ``AmpioAuthError`` if the broker rejects the credentials and
        ``AmpioConnectionError`` if nothing comes up within ``timeout``.
        A loop left running by an earlier ``open()`` is closed first: the
        two would otherwise share one client id and take the session from
        each other on every reconnect, flapping availability forever.
        """
        await self.close()
        self._stop = False
        self._closing = False
        self._connected.clear()
        self._auth_failed.clear()
        self._auth_error_message = None
        self._died.clear()
        self._fatal_message = None
        self._runner = asyncio.create_task(self._run())

        waiters = [
            asyncio.create_task(self._connected.wait()),
            asyncio.create_task(self._auth_failed.wait()),
            asyncio.create_task(self._died.wait()),
        ]
        try:
            await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
        # Connected wins even when a terminal signal raced in behind it: the
        # session did come up, so the crash/rejection is post-open news and
        # reaches the consumer through the event stream instead.
        if self._connected.is_set():
            return
        if self._auth_failed.is_set():
            await self.close()
            raise AmpioAuthError(self._auth_error_message or _AUTH_REJECTED)
        if self._died.is_set():
            await self.close()
            raise AmpioConnectionError(self._fatal_message or "Connection loop died")
        await self.close()
        raise AmpioConnectionError("Timed out connecting to Ampio")

    async def close(self) -> None:
        """Stop the loop, reporting rather than raising whatever ended it.

        A deliberate stop is not an availability event: the consumer asked
        for it, so the availability listeners are not invoked, unlike every
        other way the connection goes down. ``available`` still reads False
        afterwards.
        """
        self._closing = True
        self._stop = True
        runner, self._runner = self._runner, None
        if runner is None:
            return
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Ampio connection loop failed")

    async def publish(self, topic: str, payload: bytes) -> None:
        """Publish at QoS 1, returning once the broker acknowledges it.

        Raises ``AmpioConnectionError`` when no session is up or the
        transport fails mid-publish (the aiomqtt error chained as
        ``__cause__``), and ``AmpioTimeoutError`` when the broker accepts
        the session but the PUBACK does not arrive in time - the retryable
        shape. A consumer never sees an aiomqtt exception type.
        """
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        try:
            async with asyncio.timeout(_PUBLISH_TIMEOUT):
                await self._client.publish(topic, payload, qos=_PUBLISH_QOS)
        except TimeoutError as err:
            raise AmpioTimeoutError(
                f"Broker did not acknowledge publish to {topic} "
                f"within {_PUBLISH_TIMEOUT}s"
            ) from err
        except aiomqtt.MqttError as err:
            raise AmpioConnectionError(str(err)) from err

    async def _run(self) -> None:
        """Drive the session loop, reporting a crash instead of dying silently.

        Anything the loop does not recognize as a transport or credential
        failure is a bug: nothing will retry, so it is terminal. Left inside
        the task it would surface only when ``close()`` reaps the runner,
        and a dead loop reads exactly like an outage under retry. After a
        successful ``open()`` the crash is reported through ``on_fatal``
        (the iteration's ``finally`` has already dropped availability);
        during ``open()`` it makes ``open()`` raise instead, mirroring the
        auth path.
        """
        try:
            await self._loop()
        except Exception as err:
            _LOGGER.exception("Ampio connection loop died")
            self._stats.last_error = str(err)
            self._fatal_message = f"Connection loop died: {err}"
            self._died.set()
            if self._connected.is_set():
                self._on_fatal(self._fatal_message)

    def _default_client(self) -> aiomqtt.Client:
        return aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            identifier=self._client_id,
            timeout=10,
        )

    async def _loop(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                async with self._client_factory() as client:
                    self._client = client
                    # One SUBSCRIBE packet for the whole set - and the SUBACK
                    # verdicts are read: a broker may reject individual
                    # filters (the Ampio raw tree is admin-only), and a
                    # rejected topic that is never diagnosed reads as a
                    # mysteriously silent connection.
                    codes = await client.subscribe(
                        [(t, _SUBSCRIBE_QOS) for t in self._topics]
                    )
                    self._stats.subscribe_failures = {
                        topic: _code_value(code)
                        for topic, code in zip(self._topics, codes, strict=True)
                        if _code_value(code) >= 0x80
                    }
                    for topic, code in self._stats.subscribe_failures.items():
                        _LOGGER.warning(
                            "Ampio broker rejected subscription to %s "
                            "(reason code %d); no messages will arrive on it",
                            topic,
                            code,
                        )
                    if self._stats.started_at is None:
                        self._stats.started_at = time.time()
                    else:
                        self._stats.reconnect_count += 1
                    self._set_available(True)
                    self._connected.set()
                    attempt = 0
                    await self._on_connected()
                    async for message in client.messages:
                        self._on_message(
                            str(message.topic),
                            message.payload.decode("utf-8", "replace"),
                        )
            except (aiomqtt.MqttError, AmpioConnectionError) as err:
                # AmpioConnectionError is publish()'s wrapped form: a failure
                # inside the on-connected refresh arrives here as one and
                # recycles the session like any transport drop.
                # _is_auth_error walks the cause chain, so a wrapped auth
                # rejection still classifies.
                self._stats.last_error = str(err)
                if _is_auth_error(err):
                    # Reconnecting will not help; surface it and stop.
                    self._auth_error_message = str(err)
                    self._auth_failed.set()
                    self._stop = True
                else:
                    _LOGGER.debug("Ampio MQTT connection error: %s", err)
            finally:
                self._client = None
                self._set_available(False, notify=not self._closing)
            if self._auth_failed.is_set() and self._connected.is_set():
                # A rejection after a successful open(): the loop is stopping
                # for good and no exception will reach the caller, so this
                # callback is the only way a consumer learns it should
                # reauthenticate. Fired after the availability drop so the
                # consumer observes the connection already down. A rejection
                # on the initial connect leaves _connected unset and is
                # raised from open() instead.
                self._on_auth_failure(self._auth_error_message or _AUTH_REJECTED)
            if not self._stop:
                await asyncio.sleep(self._backoff_seconds(attempt))
                attempt += 1

    def _backoff_seconds(self, attempt: int) -> float:
        """Capped exponential backoff with jitter, in seconds.

        Caps so a long outage with many concurrent installs does not
        thunder-herd the broker on recovery. The exponent is clamped because
        attempts are unbounded: a broker down overnight would otherwise
        overflow the float and kill the retry loop.
        """
        base = self._reconnect_interval
        capped = min(_RECONNECT_BACKOFF_MAX, base * (2.0 ** min(attempt, 16)))
        return float(capped + random.uniform(0.0, base))

    def _set_available(self, available: bool, *, notify: bool = True) -> None:
        if available == self._available:
            return
        self._available = available
        if notify:
            self._on_availability(available)


async def probe(
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    *,
    request_topic: str,
    request_payload: str,
    reply_topic: str,
    timeout: float,
    client_factory: MqttClientFactory | None = None,
) -> str | None:
    """Connect once, ask for one reply, and return its payload.

    Used by the config-flow helper: it answers "do these credentials work, and
    what does the server say it is" without starting the reconnect loop.
    Returns None when the connection is fine but nothing answers in time.
    """
    factory = client_factory or (
        lambda: aiomqtt.Client(
            hostname=host,
            port=port,
            username=username,
            password=password,
            identifier=f"ampio_mqtt_test_{uuid.uuid4().hex}",
            timeout=10,
        )
    )
    try:
        async with factory() as client:
            await client.subscribe(reply_topic, qos=_SUBSCRIBE_QOS)
            await client.publish(
                request_topic, request_payload.encode(), qos=_PUBLISH_QOS
            )
            with suppress(TimeoutError):
                async with asyncio.timeout(timeout):
                    async for message in client.messages:
                        if str(message.topic) == reply_topic:
                            return message.payload.decode("utf-8", "replace")
    except aiomqtt.MqttError as err:
        if _is_auth_error(err):
            raise AmpioAuthError(str(err)) from err
        raise AmpioConnectionError(str(err)) from err
    return None


def _is_auth_error(err: BaseException) -> bool:
    """Whether an MQTT failure is a credential rejection rather than transport.

    Reads the structured reason code off ``MqttCodeError`` instead of matching
    error text. The cause chain is walked because a drop that surfaces during
    message iteration arrives as a bare ``MqttError`` with the coded
    disconnect error chained as its ``__cause__``. Codes outside
    ``_AUTH_REASON_CODES`` never false-match: paho's own ``MQTTErrorCode``
    ints stay in single digits (where 5 is "connection refused", an auth code
    only in raw MQTT 3.1.1 CONNACK numbering, which VERSION2 callbacks never
    surface).
    """
    current: BaseException | None = err
    while current is not None:
        if (
            isinstance(current, aiomqtt.MqttCodeError)
            and current.rc in _AUTH_REASON_CODES
        ):
            return True
        current = current.__cause__
    return False


def _code_value(code: object) -> int:
    """A SUBACK entry as an int, whichever shape paho handed over.

    VERSION2 callbacks deliver ``ReasonCode`` (v5-normalized on every wire
    protocol); older paths deliver plain granted-QoS ints. Both put the
    failure bit at 0x80. An unreadable entry counts as failed rather than
    granted.
    """
    value = getattr(code, "value", code)
    return value if isinstance(value, int) else 0x80
