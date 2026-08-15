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

from . import _protocol
from .errors import AmpioAuthError, AmpioConnectionError
from .models import ConnectionStats

_LOGGER = logging.getLogger(__name__)

_RECONNECT_BACKOFF_MAX = 60.0
_AUTH_REJECTED = "Authentication rejected by Ampio broker"

MessageHandler = Callable[[str, str], None]
AvailabilityHandler = Callable[[bool], None]
ConnectedHandler = Callable[[], Awaitable[None]]
AuthFailureHandler = Callable[[str], None]


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

        # Reusing one client id across reconnects keeps the broker from seeing
        # parallel "ghost" sessions while the previous one expires.
        self._client_id = f"ampio_mqtt_{uuid.uuid4().hex}"
        self._client: aiomqtt.Client | None = None
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._auth_failed = asyncio.Event()
        self._auth_error_message: str | None = None
        self._available = False
        self._stop = False

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
        """
        self._stop = False
        self._connected.clear()
        self._auth_failed.clear()
        self._auth_error_message = None
        self._runner = asyncio.create_task(self._run())

        waiters = [
            asyncio.create_task(self._connected.wait()),
            asyncio.create_task(self._auth_failed.wait()),
        ]
        try:
            done, _ = await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
        if not done:
            await self.close()
            raise AmpioConnectionError("Timed out connecting to Ampio")
        if self._auth_failed.is_set():
            await self.close()
            raise AmpioAuthError(self._auth_error_message or _AUTH_REJECTED)

    async def close(self) -> None:
        """Stop the loop, reporting rather than raising whatever ended it."""
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
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        await self._client.publish(topic, payload)

    async def _run(self) -> None:
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
                    for topic in self._topics:
                        await client.subscribe(topic)
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
                            str(message.topic), _decode_payload(message.payload)
                        )
            except aiomqtt.MqttError as err:
                self._stats.last_error = str(err)
                if _protocol.is_auth_error(err):
                    # Reconnecting will not help; surface it and stop.
                    self._auth_error_message = str(err)
                    self._auth_failed.set()
                    self._stop = True
                else:
                    _LOGGER.debug("Ampio MQTT connection error: %s", err)
            finally:
                self._client = None
                self._set_available(False)
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

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
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
) -> str | None:
    """Connect once, ask for one reply, and return its payload.

    Used by the config-flow helper: it answers "do these credentials work, and
    what does the server say it is" without starting the reconnect loop.
    Returns None when the connection is fine but nothing answers in time.
    """
    try:
        async with aiomqtt.Client(
            hostname=host,
            port=port,
            username=username,
            password=password,
            identifier=f"ampio_mqtt_test_{uuid.uuid4().hex}",
            timeout=10,
        ) as client:
            await client.subscribe(reply_topic)
            await client.publish(request_topic, request_payload.encode())
            with suppress(TimeoutError):
                async with asyncio.timeout(timeout):
                    async for message in client.messages:
                        if str(message.topic) == reply_topic:
                            return _decode_payload(message.payload)
    except aiomqtt.MqttError as err:
        if _protocol.is_auth_error(err):
            raise AmpioAuthError(str(err)) from err
        raise AmpioConnectionError(str(err)) from err
    return None


def _decode_payload(payload: object) -> str:
    """Coerce an aiomqtt payload (`str | bytes | bytearray | None`) to text."""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", "replace")
    if isinstance(payload, str):
        return payload
    return ""
