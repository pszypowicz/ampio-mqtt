"""Tests for the locations-fetching path.

`AmpioClient.fetch_locations()` returns the Designer "Location" marker
name table: the integer id -> human label dropdown that Designer
populates the per-output location column from. The wire shape is
``{"List": [{"id": int, "opis_menu": str}, ...]}`` on
``ampio/fromDB/<user>/config/locations``, triggered by publishing
``locations`` on ``ampio/control/<user>/config``.
"""

from __future__ import annotations

import asyncio
import json

import aiomqtt
import pytest

from ampio_mqtt import AmpioClient, AmpioConnectionError


class _FakeMqttClient:
    """Minimal aiomqtt.Client stand-in: records publishes, no real broker."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, payload: bytes) -> None:
        self.published.append((topic, payload))


async def test_fetch_locations_raises_when_not_connected() -> None:
    client = AmpioClient("host", username="u", password="p")
    with pytest.raises(AmpioConnectionError):
        await client.fetch_locations()


async def test_fetch_locations_publishes_keyword_and_parses_response() -> None:
    client = AmpioClient("host", username="u", password="p")
    fake_broker = _FakeMqttClient()
    client._connection._client = fake_broker  # type: ignore[assignment]

    payload = json.dumps(
        {
            "List": [
                {"id": 1, "opis_menu": "Salon"},
                {"id": 2, "opis_menu": "Kuchnia"},
                {"id": 21, "opis_menu": "marker_full_capture_777"},
            ]
        }
    )

    async def _deliver() -> None:
        await asyncio.sleep(0)
        client._feed_message("ampio/fromDB/u/config/locations", payload)

    delivery = asyncio.create_task(_deliver())
    try:
        result = await client.fetch_locations(timeout=1.0)
    finally:
        await delivery

    assert result == {1: "Salon", 2: "Kuchnia", 21: "marker_full_capture_777"}
    assert fake_broker.published == [("ampio/control/u/config", b"locations")]
    assert client.last_payloads["locations"] == payload


async def test_fetch_locations_times_out_when_response_missing() -> None:
    client = AmpioClient("host", username="u", password="p")
    client._connection._client = _FakeMqttClient()  # type: ignore[assignment]
    with pytest.raises(AmpioConnectionError):
        await client.fetch_locations(timeout=0.05)


async def test_fetch_locations_skips_malformed_entries() -> None:
    client = AmpioClient("host", username="u", password="p")
    client._connection._client = _FakeMqttClient()  # type: ignore[assignment]

    payload = json.dumps(
        {
            "List": [
                {"id": 1, "opis_menu": "OK"},
                {"id": None, "opis_menu": "no id"},
                {"id": 2, "opis_menu": ""},
                {"id": 3, "opis_menu": None},
                "not an object",
                {"id": "4", "opis_menu": "wrong id type"},
                {"id": 5, "opis_menu": "Used"},
            ]
        }
    )

    async def _deliver() -> None:
        await asyncio.sleep(0)
        client._feed_message("ampio/fromDB/u/config/locations", payload)

    delivery = asyncio.create_task(_deliver())
    try:
        result = await client.fetch_locations(timeout=1.0)
    finally:
        await delivery
    assert result == {1: "OK", 5: "Used"}


async def test_fetch_locations_recovers_from_malformed_response() -> None:
    """A garbage payload yields an empty dict, not a crash."""
    client = AmpioClient("host", username="u", password="p")
    client._connection._client = _FakeMqttClient()  # type: ignore[assignment]

    async def _deliver() -> None:
        await asyncio.sleep(0)
        client._feed_message("ampio/fromDB/u/config/locations", "not-json")

    delivery = asyncio.create_task(_deliver())
    try:
        result = await client.fetch_locations(timeout=1.0)
    finally:
        await delivery
    assert result == {}


async def test_fetch_locations_clears_state_between_calls() -> None:
    client = AmpioClient("host", username="u", password="p")
    client._connection._client = _FakeMqttClient()  # type: ignore[assignment]

    async def _deliver(payload: str) -> None:
        await asyncio.sleep(0)
        client._feed_message("ampio/fromDB/u/config/locations", payload)

    first = asyncio.create_task(
        _deliver(json.dumps({"List": [{"id": 1, "opis_menu": "A"}]}))
    )
    r1 = await client.fetch_locations(timeout=1.0)
    await first
    assert r1 == {1: "A"}

    second = asyncio.create_task(
        _deliver(json.dumps({"List": [{"id": 2, "opis_menu": "B"}]}))
    )
    r2 = await client.fetch_locations(timeout=1.0)
    await second
    assert r2 == {2: "B"}


async def test_fetch_locations_propagates_publish_failure() -> None:
    class _RaisingClient:
        async def publish(self, topic: str, payload: bytes) -> None:
            raise aiomqtt.MqttError("publish failed")

    client = AmpioClient("host", username="u", password="p")
    client._connection._client = _RaisingClient()  # type: ignore[assignment]
    with pytest.raises(aiomqtt.MqttError):
        await client.fetch_locations(timeout=1.0)
