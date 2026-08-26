"""Tests for bus events (`ampio/from/<MAC>/event` and `/api/setEvent`) and
the `ObjectAdded` catalogue-creation event."""

from __future__ import annotations

import pytest
from conftest import API_TOPIC, USER, FakeBroker, details, feed

from ampio_mqtt import (
    AmpioClient,
    AmpioConnectionError,
    BusEvent,
    ObjectAdded,
    ObjectUpdated,
)


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


async def test_object_added_flows_through_both_filters() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        updated: list[ObjectUpdated] = []
        added: list[ObjectAdded] = []
        client.subscribe(updated.append, of=ObjectUpdated)
        client.subscribe(added.append, of=ObjectAdded)
        feed(
            client,
            "ampio/fromDB/u/data/devices",
            details({"id": 5, "typ_komponentu": "flaga"}),
        )
        assert [type(e) for e in added] == [ObjectAdded]
        # The subclass relationship keeps existing subscriptions whole.
        assert [type(e) for e in updated] == [ObjectAdded]
        feed(
            client,
            "ampio/fromDB/u/data/devices",
            details({"id": 5, "typ_komponentu": "flaga", "opis_menu": "x"}),
        )
        assert [type(e) for e in added] == [ObjectAdded]
        assert [type(e) for e in updated] == [ObjectAdded, ObjectUpdated]
    finally:
        await client.stop()


async def test_object_added_object_id_filter() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        events: list[ObjectAdded] = []
        client.subscribe(events.append, of=ObjectAdded, object_id=5)
        feed(
            client,
            "ampio/fromDB/u/data/devices",
            details(
                {"id": 5, "typ_komponentu": "flaga"},
                {"id": 6, "typ_komponentu": "flaga"},
            ),
        )
        assert [e.object.id for e in events] == [5]
    finally:
        await client.stop()


async def test_reconnect_replay_does_not_redispatch_object_added() -> None:
    """A reconnect re-requests the catalogue; replaying an already-known
    row must not re-fire `ObjectAdded` for it."""
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        feed(
            client,
            "ampio/fromDB/u/data/devices",
            details({"id": 5, "typ_komponentu": "flaga"}),
        )
        added: list[ObjectAdded] = []
        client.subscribe(added.append, of=ObjectAdded)

        # A reconnect's replay of the same catalogue must not re-add object 5.
        feed(
            client,
            "ampio/fromDB/u/data/devices",
            details({"id": 5, "typ_komponentu": "flaga"}),
        )
        assert added == []

        # A genuinely new row (6) alongside the known one (5) adds only 6.
        feed(
            client,
            "ampio/fromDB/u/data/devices",
            details(
                {"id": 5, "typ_komponentu": "flaga"},
                {"id": 6, "typ_komponentu": "flaga"},
            ),
        )
        assert [e.object.id for e in added] == [6]
    finally:
        await client.stop()
