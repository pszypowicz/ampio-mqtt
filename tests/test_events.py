"""Tests for bus events (`ampio/from/<MAC>/event` and `/api/setEvent`)."""

from __future__ import annotations

import pytest
from conftest import API_TOPIC, USER, FakeBroker, feed

from ampio_mqtt import AmpioClient, AmpioConnectionError, BusEvent


def test_received_event_reaches_listeners() -> None:
    """The originator mac is the sending module's, hex-parsed off the topic."""
    client = AmpioClient("host", username=USER)
    seen: list[BusEvent] = []
    client.subscribe(seen.append, of=BusEvent)

    feed(client, "ampio/from/D09A/event", b"42")

    assert seen == [BusEvent(number=42, mac=0xD09A)]


def test_a_raw_channel_message_is_not_a_bus_event() -> None:
    client = AmpioClient("host", username=USER)
    seen: list[BusEvent] = []
    client.subscribe(seen.append, of=BusEvent)
    feed(client, "ampio/from/1/state/f/2", b"189")
    assert seen == []


async def test_send_event_builds_the_payload(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    await client.send_event(189)
    assert broker.published == [(API_TOPIC, b"/api/setEvent/189")]


@pytest.mark.parametrize("number", [0, -1, 65536])
async def test_event_number_range_is_checked(
    connected: tuple[AmpioClient, FakeBroker], number: int
) -> None:
    client, broker = connected
    with pytest.raises(ValueError):
        await client.send_event(number)
    assert broker.published == []


async def test_send_event_requires_a_connection() -> None:
    client = AmpioClient("host", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.send_event(189)
