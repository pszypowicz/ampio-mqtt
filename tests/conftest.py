"""Shared test kit: the fake broker, client helpers, and payload builders.

The suite talks to the library through two sanctioned doors only:

- the ``mqtt_client_factory`` transport seam, fed a :class:`FakeBroker`
  instance (no aiomqtt patching, no class-level state), and
- :func:`feed`, the single blessed reach into the private dispatch entry,
  kept synchronous so message-driven tests stay fast and sleep-free.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Self

import pytest

from ampio_mqtt import AmpioClient

USER = "u"

DETAILS_TOPIC = f"ampio/fromDB/{USER}/config/devicesDetails"
DEVICES_TOPIC = f"ampio/fromDB/{USER}/config/devices"
STATES_TOPIC = f"ampio/fromDB/{USER}/data/states"
INFO_TOPIC = f"ampio/fromDB/{USER}/data/info"
DATA_DEVICES_TOPIC = f"ampio/fromDB/{USER}/data/devices"
PARAMS_DEVICES_TOPIC = f"ampio/fromDB/{USER}/data/params_devices"
API_TOPIC = f"ampio/control/{USER}/api"


class Message:
    """Minimal stand-in for `aiomqtt.Message`."""

    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeBroker:
    """Instance-based `aiomqtt.Client` stand-in for the transport seam.

    Pass ``broker.factory`` as ``mqtt_client_factory``; the same instance
    serves every reconnect. Scripted connect outcomes (`enter_errors`) and
    publish outcomes (`publish_errors`) are consumed left to right;
    `scripted_messages` replay into the stream on every connect, and
    `deliver` queues into the live session.
    """

    def __init__(self) -> None:
        self.enter_error: BaseException | None = None
        self.enter_errors: list[BaseException | None] = []
        self.enter_delay: float = 0.0
        # Raised from the message stream once queued messages are consumed,
        # simulating the broker dropping an established connection.
        self.stream_error: BaseException | None = None
        self.publish_errors: list[BaseException | None] = []
        self.scripted_messages: list[Message] = []
        self.published: list[tuple[str, bytes]] = []
        self.published_qos: list[int] = []
        self.subscribed: list[str] = []
        self.subscribed_qos: list[int] = []
        # Per-topic SUBACK reason codes; topics absent here are granted (0).
        self.suback_codes: dict[str, int] = {}
        self._queue: asyncio.Queue[Message] = asyncio.Queue()

    def factory(self) -> Self:
        """The `mqtt_client_factory` handing this instance to each connect."""
        return self

    async def __aenter__(self) -> Self:
        if self.enter_delay:
            await asyncio.sleep(self.enter_delay)
        error = self.enter_errors.pop(0) if self.enter_errors else self.enter_error
        if error is not None:
            raise error
        self._queue = asyncio.Queue()
        for msg in self.scripted_messages:
            self._queue.put_nowait(msg)
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def subscribe(
        self, topic: str | list[tuple[str, int]], qos: int = 0
    ) -> list[int]:
        entries = topic if isinstance(topic, list) else [(topic, qos)]
        for t, q in entries:
            self.subscribed.append(t)
            self.subscribed_qos.append(q)
        return [self.suback_codes.get(t, 0) for t, _q in entries]

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 0) -> None:
        error = self.publish_errors.pop(0) if self.publish_errors else None
        if error is not None:
            raise error
        self.published.append((topic, payload))
        self.published_qos.append(qos)

    def deliver(self, topic: str, payload: bytes) -> None:
        """Queue a message for the running session's stream."""
        self._queue.put_nowait(Message(topic, payload))

    @property
    def messages(self) -> Self:
        return self

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Message:
        if self.stream_error is not None and self._queue.empty():
            raise self.stream_error
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=5.0)
        except TimeoutError as err:
            raise StopAsyncIteration from err


def feed(client: AmpioClient, topic: str, payload: bytes | str) -> None:
    """Inject one message into the client's dispatch synchronously."""
    raw = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
    client._handle_message(topic, raw)


@pytest.fixture
async def connected() -> AsyncIterator[tuple[AmpioClient, FakeBroker]]:
    """A started client on a FakeBroker, publishes cleared of discovery.

    For command/fetch tests: assertions read ``broker.published`` and see
    only what the test itself caused.
    """
    broker = FakeBroker()
    client = AmpioClient("host", username=USER, mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    broker.published.clear()
    broker.published_qos.clear()
    yield client, broker
    await client.stop()


def details(*items: dict) -> bytes:
    """A `devicesDetails` / `data/devices` catalogue payload."""
    return json.dumps({"Status": 0, "List": list(items)}).encode()


def devices(*items: dict) -> bytes:
    """A `devices` module-list payload."""
    return json.dumps({"List": list(items)}).encode()


def info(**fields: object) -> bytes:
    """A server-info payload."""
    return json.dumps({"Results": fields}).encode()
