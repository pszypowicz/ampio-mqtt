"""Tests for the Designer locations name table (config/locations)."""

from __future__ import annotations

import json

import pytest
from conftest import ADMIN_USER, FakeBroker, deliver_later

from ampio_mqtt import AmpioClient, AmpioProtocolError
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


@pytest.mark.parametrize("column", ["id", "opis_menu"])
def test_parse_locations_refuses_a_row_without_a_served_column(column: str) -> None:
    """Every row of the name table carries both columns, and a name the
    pointer cannot resolve would read as an unassigned location."""
    row = {"id": 1, "opis_menu": "OK"}
    del row[column]
    with pytest.raises(AmpioProtocolError, match=column):
        parse_locations(json.dumps({"List": [row]}))


@pytest.mark.parametrize("value", [None, ""])
def test_parse_locations_refuses_an_unusable_name(value: object) -> None:
    payload = json.dumps({"List": [{"id": 1, "opis_menu": value}]})
    with pytest.raises(AmpioProtocolError, match="opis_menu"):
        parse_locations(payload)


@pytest.mark.parametrize("payload", ["not-json", json.dumps({"Status": 0})])
def test_parse_locations_refuses_a_reply_of_the_wrong_shape(payload: str) -> None:
    with pytest.raises(AmpioProtocolError):
        parse_locations(payload)


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
