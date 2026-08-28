"""Tests for the Designer locations name table (config/locations)."""

from __future__ import annotations

import json

import pytest
from conftest import ADMIN_USER, FakeBroker, deliver_later

from ampio_mqtt import AmpioClient
from ampio_mqtt._protocol import parse_locations

LOCATIONS_TOPIC = f"ampio/fromDB/{ADMIN_USER}/config/locations"


def test_parse_locations_happy_path() -> None:
    payload = json.dumps(
        {
            "List": [
                {"id": 14, "opis_menu": "Potter"},
                {"id": 19, "opis_menu": "Testowe"},
            ]
        }
    )
    assert parse_locations(payload) == {14: "Potter", 19: "Testowe"}


def test_parse_locations_skips_malformed_rows() -> None:
    payload = json.dumps(
        {
            "List": [
                {"id": 1, "opis_menu": "OK"},
                {"id": None, "opis_menu": "x"},
                {"id": 2, "opis_menu": ""},
                "not a dict",
            ]
        }
    )
    assert parse_locations(payload) == {1: "OK"}


def test_parse_locations_rejects_non_list_payload() -> None:
    assert parse_locations("not-json") is None
    assert parse_locations(json.dumps({"Status": 0})) is None


async def test_fetch_locations_requests_and_parses() -> None:
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.connect(timeout=2.0, discovery_timeout=0.01)
    broker.published.clear()
    try:
        delivery = deliver_later(
            client,
            (
                LOCATIONS_TOPIC,
                json.dumps({"List": [{"id": 14, "opis_menu": "Potter"}]}),
            ),
        )
        try:
            result = await client.fetch_locations(timeout=1.0)
        finally:
            await delivery
        assert result == {14: "Potter"}
        assert (f"ampio/control/{ADMIN_USER}/config", b"locations") in broker.published
    finally:
        await client.disconnect()


async def test_fetch_locations_raises_on_restricted_tier() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.connect(timeout=2.0, discovery_timeout=0.01)
    try:
        with pytest.raises(RuntimeError, match="restricted"):
            await client.fetch_locations(timeout=0.1)
    finally:
        await client.disconnect()
