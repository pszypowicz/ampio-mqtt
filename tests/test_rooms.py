"""Tests for the room-fetching path.

Two orthogonal layers:

- `join_rooms()` in `ampio_mqtt.rooms` - pure join of the two M-SERV
  payloads. Tested directly with synthetic dicts.
- `AmpioClient.fetch_rooms()` - the MQTT request/response orchestration.
  Exercised with the same `_FakeAiomqtt` helpers the existing client tests
  use: messages are fed into the client via the private `_feed_message`
  hook and the broker-side publish is intercepted via a fake aiomqtt
  client.
"""

from __future__ import annotations

import asyncio
import json

import aiomqtt
import pytest

from ampio_mqtt import AmpioClient, AmpioConnectionError
from ampio_mqtt.rooms import join_rooms

# --- join_rooms() pure tests ----------------------------------------------


def test_join_rooms_happy_path() -> None:
    groups = {
        "List": [
            {"id": 8, "id_rodzica": 4, "opis_menu": "Salon"},
            {"id": 7, "id_rodzica": 4, "opis_menu": "Jadalnia"},
        ]
    }
    group_devices = {
        "List": [
            {"id_grupy": 8, "id_obiektu": 31},
            {"id_grupy": 7, "id_obiektu": 28},
        ]
    }
    assert join_rooms(groups, group_devices) == {31: "Salon", 28: "Jadalnia"}


def test_join_rooms_first_match_wins_for_multi_group_objects() -> None:
    """Object 50 appears in groups 15 (Schody) and 11 (Korytarz) - first wins."""
    groups = {
        "List": [
            {"id": 15, "opis_menu": "Schody"},
            {"id": 11, "opis_menu": "Korytarz"},
        ]
    }
    group_devices = {
        "List": [
            {"id_grupy": 15, "id_obiektu": 50},
            {"id_grupy": 11, "id_obiektu": 50},
        ]
    }
    assert join_rooms(groups, group_devices) == {50: "Schody"}


def test_join_rooms_skips_malformed_entries() -> None:
    groups = {
        "List": [
            {"id": 1, "opis_menu": "OK"},
            {"id": None, "opis_menu": "Missing id"},
            {"id": 2, "opis_menu": ""},
            {"id": 3, "opis_menu": None},
            {"id": 4, "opis_menu": "Used"},
        ]
    }
    group_devices = {
        "List": [
            {"id_grupy": 1, "id_obiektu": 100},
            {"id_grupy": 2, "id_obiektu": 101},
            {"id_grupy": 3, "id_obiektu": 102},
            {"id_grupy": 4, "id_obiektu": None},
            {"id_grupy": None, "id_obiektu": 103},
            {"id_grupy": 4, "id_obiektu": 104},
        ]
    }
    assert join_rooms(groups, group_devices) == {100: "OK", 104: "Used"}


def test_join_rooms_ignores_devices_pointing_at_unknown_groups() -> None:
    groups = {"List": [{"id": 1, "opis_menu": "OK"}]}
    group_devices = {"List": [{"id_grupy": 99, "id_obiektu": 5}]}
    assert join_rooms(groups, group_devices) == {}


def test_join_rooms_empty_inputs() -> None:
    assert join_rooms({}, {}) == {}
    assert join_rooms({"List": []}, {"List": []}) == {}


# --- AmpioClient.fetch_rooms() MQTT orchestration -------------------------


class _FakeMqttClient:
    """Minimal aiomqtt.Client stand-in: records publishes, no real broker."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, payload: bytes) -> None:
        self.published.append((topic, payload))


async def test_fetch_rooms_raises_when_not_connected() -> None:
    client = AmpioClient("host", username="u", password="p")
    with pytest.raises(AmpioConnectionError):
        await client.fetch_rooms()


async def test_fetch_rooms_publishes_keywords_and_joins_responses() -> None:
    client = AmpioClient("host", username="u", password="p")
    fake_broker = _FakeMqttClient()
    client._client = fake_broker  # type: ignore[assignment]

    groups_payload = json.dumps(
        {"List": [{"id": 8, "opis_menu": "Salon"}, {"id": 7, "opis_menu": "Jadalnia"}]}
    )
    group_devices_payload = json.dumps(
        {
            "List": [
                {"id_grupy": 8, "id_obiektu": 31},
                {"id_grupy": 7, "id_obiektu": 28},
            ]
        }
    )

    async def _deliver_responses() -> None:
        # Give fetch_rooms a turn to publish before the responses arrive.
        await asyncio.sleep(0)
        client._feed_message("ampio/fromDB/u/data/groups", groups_payload)
        client._feed_message("ampio/fromDB/u/data/group_devices", group_devices_payload)

    delivery = asyncio.create_task(_deliver_responses())
    try:
        result = await client.fetch_rooms(timeout=1.0)
    finally:
        await delivery

    assert result == {31: "Salon", 28: "Jadalnia"}
    assert fake_broker.published == [
        ("ampio/control/u/data", b"groups"),
        ("ampio/control/u/data", b"group_devices"),
    ]


async def test_fetch_rooms_times_out_when_response_missing() -> None:
    client = AmpioClient("host", username="u", password="p")
    client._client = _FakeMqttClient()  # type: ignore[assignment]
    # Deliver only one of the two responses - groups arrives, group_devices never does.

    async def _deliver_partial() -> None:
        await asyncio.sleep(0)
        client._feed_message("ampio/fromDB/u/data/groups", json.dumps({"List": []}))

    delivery = asyncio.create_task(_deliver_partial())
    try:
        with pytest.raises(AmpioConnectionError):
            await client.fetch_rooms(timeout=0.1)
    finally:
        await delivery


async def test_fetch_rooms_recovers_from_malformed_response() -> None:
    """A garbage payload yields an empty join, not a crash."""
    client = AmpioClient("host", username="u", password="p")
    client._client = _FakeMqttClient()  # type: ignore[assignment]

    async def _deliver_garbage() -> None:
        await asyncio.sleep(0)
        client._feed_message("ampio/fromDB/u/data/groups", "not-json")
        client._feed_message("ampio/fromDB/u/data/group_devices", "[]")

    delivery = asyncio.create_task(_deliver_garbage())
    try:
        result = await client.fetch_rooms(timeout=1.0)
    finally:
        await delivery
    assert result == {}


async def test_fetch_rooms_clears_state_between_calls() -> None:
    """A second call must not see stale events from the first."""
    client = AmpioClient("host", username="u", password="p")
    client._client = _FakeMqttClient()  # type: ignore[assignment]

    async def _deliver(groups: str, group_devices: str) -> None:
        await asyncio.sleep(0)
        client._feed_message("ampio/fromDB/u/data/groups", groups)
        client._feed_message("ampio/fromDB/u/data/group_devices", group_devices)

    first = asyncio.create_task(
        _deliver(
            json.dumps({"List": [{"id": 1, "opis_menu": "A"}]}),
            json.dumps({"List": [{"id_grupy": 1, "id_obiektu": 10}]}),
        )
    )
    r1 = await client.fetch_rooms(timeout=1.0)
    await first
    assert r1 == {10: "A"}

    second = asyncio.create_task(
        _deliver(
            json.dumps({"List": [{"id": 2, "opis_menu": "B"}]}),
            json.dumps({"List": [{"id_grupy": 2, "id_obiektu": 20}]}),
        )
    )
    r2 = await client.fetch_rooms(timeout=1.0)
    await second
    assert r2 == {20: "B"}


async def test_fetch_rooms_propagates_publish_failure() -> None:
    """If the broker raises while publishing the request, we surface it."""

    class _RaisingClient:
        async def publish(self, topic: str, payload: bytes) -> None:
            raise aiomqtt.MqttError("publish failed")

    client = AmpioClient("host", username="u", password="p")
    client._client = _RaisingClient()  # type: ignore[assignment]

    with pytest.raises(aiomqtt.MqttError):
        await client.fetch_rooms(timeout=1.0)
