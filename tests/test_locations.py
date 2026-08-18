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
from conftest import FakeBroker, feed

from ampio_mqtt import AmpioClient, AmpioConnectionError, AmpioTimeoutError


async def test_fetch_locations_raises_when_not_connected() -> None:
    client = AmpioClient("host", username="u", password="p")
    with pytest.raises(AmpioConnectionError):
        await client.fetch_locations()


async def test_fetch_locations_publishes_keyword_and_parses_response(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected

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
        feed(client, "ampio/fromDB/u/config/locations", payload)

    delivery = asyncio.create_task(_deliver())
    try:
        result = await client.fetch_locations(timeout=1.0)
    finally:
        await delivery

    assert result == {1: "Salon", 2: "Kuchnia", 21: "marker_full_capture_777"}
    assert broker.published == [("ampio/control/u/config", b"locations")]
    assert client.last_payloads["locations"] == payload


async def test_fetch_locations_times_out_when_response_missing(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, _ = connected
    with pytest.raises(AmpioConnectionError):
        await client.fetch_locations(timeout=0.05)


async def test_fetch_locations_skips_malformed_entries(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, _ = connected

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
        feed(client, "ampio/fromDB/u/config/locations", payload)

    delivery = asyncio.create_task(_deliver())
    try:
        result = await client.fetch_locations(timeout=1.0)
    finally:
        await delivery
    assert result == {1: "OK", 5: "Used"}


async def test_fetch_locations_treats_malformed_response_as_no_response(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A corrupt reply must end in the retryable timeout, not a fake-valid
    empty table."""
    client, _ = connected

    async def _deliver() -> None:
        await asyncio.sleep(0)
        feed(client, "ampio/fromDB/u/config/locations", "not-json")

    delivery = asyncio.create_task(_deliver())
    try:
        with pytest.raises(AmpioTimeoutError):
            await client.fetch_locations(timeout=0.1)
    finally:
        await delivery


async def test_fetch_locations_clears_state_between_calls(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, _ = connected

    async def _deliver(payload: str) -> None:
        await asyncio.sleep(0)
        feed(client, "ampio/fromDB/u/config/locations", payload)

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


async def test_fetch_locations_wraps_publish_failure(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    broker.publish_errors = [aiomqtt.MqttError("publish failed")]
    with pytest.raises(AmpioConnectionError) as excinfo:
        await client.fetch_locations(timeout=1.0)
    assert isinstance(excinfo.value.__cause__, aiomqtt.MqttError)
