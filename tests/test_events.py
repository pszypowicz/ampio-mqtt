"""Tests for bus events (`ampio/from/<MAC>/event` and `/api/setEvent`)."""

from __future__ import annotations

import pytest

from ampio_mqtt import AmpioClient, AmpioConnectionError, BusEvent

USER = "u"
TOPIC = f"ampio/control/{USER}/api"


class _RecordingClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 0) -> None:
        self.published.append((topic, payload))


def _connected() -> tuple[AmpioClient, _RecordingClient]:
    client = AmpioClient("host", username=USER)
    recorder = _RecordingClient()
    client._connection._client = recorder  # type: ignore[assignment]
    return client, recorder


def test_received_event_reaches_listeners() -> None:
    client = AmpioClient("host", username=USER)
    seen: list[BusEvent] = []
    client.subscribe(seen.append, of=BusEvent)

    client._feed_message("ampio/from/1/event", b"189")

    assert seen == [BusEvent(number=189, mac=1)]


def test_event_mac_identifies_the_originator() -> None:
    """A panel press carries that module's mac, not the M-SERV's."""
    client = AmpioClient("host", username=USER)
    seen: list[BusEvent] = []
    client.subscribe(seen.append, of=BusEvent)

    client._feed_message("ampio/from/D09A/event", b"42")

    assert seen == [BusEvent(number=42, mac=0xD09A)]


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("ampio/from/1/event", b"not-a-number"),
        ("ampio/from/zz/event", b"189"),
        ("ampio/from/1/state/f/2", b"189"),
    ],
)
def test_malformed_events_are_ignored(topic: str, payload: bytes) -> None:
    client = AmpioClient("host", username=USER)
    seen: list[BusEvent] = []
    client.subscribe(seen.append, of=BusEvent)
    client._feed_message(topic, payload)
    assert seen == []


def test_event_listener_can_be_removed() -> None:
    client = AmpioClient("host", username=USER)
    seen: list[BusEvent] = []
    unsubscribe = client.subscribe(seen.append, of=BusEvent)
    unsubscribe()
    client._feed_message("ampio/from/1/event", b"189")
    assert seen == []


async def test_send_event_builds_the_payload() -> None:
    client, recorder = _connected()
    await client.send_event(189)
    assert recorder.published == [(TOPIC, b"/api/setEvent/189")]


@pytest.mark.parametrize("number", [0, -1, 65536])
async def test_event_number_range_is_checked(number: int) -> None:
    client, recorder = _connected()
    with pytest.raises(ValueError):
        await client.send_event(number)
    assert recorder.published == []


async def test_send_event_requires_a_connection() -> None:
    client = AmpioClient("host", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.send_event(189)
