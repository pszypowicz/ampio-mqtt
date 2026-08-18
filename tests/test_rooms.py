"""Tests for the room-fetching path.

Two orthogonal layers:

- `parse_rooms()` in `ampio_mqtt._protocol` - pure join of the two M-SERV
  reply payloads.
- `AmpioClient.fetch_rooms()` - the MQTT request/response orchestration.
  Exercised with the shared FakeBroker kit: replies are injected via
  `feed` and the broker-side publishes are read off the fake broker.
"""

from __future__ import annotations

import asyncio
import json

import aiomqtt
import pytest
from conftest import FakeBroker, deliver_later, feed

from ampio_mqtt import AmpioClient, AmpioConnectionError, AmpioTimeoutError
from ampio_mqtt._protocol import parse_rooms

# --- parse_rooms() pure tests ----------------------------------------------


def _payload(rows: list[object]) -> str:
    return json.dumps({"List": rows})


def test_parse_rooms_happy_path() -> None:
    groups = [
        {"id": 8, "id_rodzica": 4, "opis_menu": "Salon"},
        {"id": 7, "id_rodzica": 4, "opis_menu": "Jadalnia"},
    ]
    group_devices = [
        {"id_grupy": 8, "id_obiektu": 31},
        {"id_grupy": 7, "id_obiektu": 28},
    ]
    assert parse_rooms(groups, group_devices) == {31: "Salon", 28: "Jadalnia"}


def test_parse_rooms_first_match_wins_for_multi_group_objects() -> None:
    """Object 50 appears in groups 15 (Schody) and 11 (Korytarz) - first wins."""
    groups = [
        {"id": 15, "opis_menu": "Schody"},
        {"id": 11, "opis_menu": "Korytarz"},
    ]
    group_devices = [
        {"id_grupy": 15, "id_obiektu": 50},
        {"id_grupy": 11, "id_obiektu": 50},
    ]
    assert parse_rooms(groups, group_devices) == {50: "Schody"}


def test_parse_rooms_skips_malformed_entries() -> None:
    groups = [
        {"id": 1, "opis_menu": "OK"},
        {"id": None, "opis_menu": "Missing id"},
        {"id": 2, "opis_menu": ""},
        {"id": 3, "opis_menu": None},
        "not an object",
        {"id": 4, "opis_menu": "Used"},
    ]
    group_devices = [
        {"id_grupy": 1, "id_obiektu": 100},
        {"id_grupy": 2, "id_obiektu": 101},
        {"id_grupy": 3, "id_obiektu": 102},
        {"id_grupy": 4, "id_obiektu": None},
        {"id_grupy": None, "id_obiektu": 103},
        "not an object",
        {"id_grupy": 4, "id_obiektu": 104},
    ]
    assert parse_rooms(groups, group_devices) == {100: "OK", 104: "Used"}


def test_parse_rooms_ignores_devices_pointing_at_unknown_groups() -> None:
    groups = [{"id": 1, "opis_menu": "OK"}]
    group_devices = [{"id_grupy": 99, "id_obiektu": 5}]
    assert parse_rooms(groups, group_devices) == {}


def test_parse_rooms_of_empty_tables_is_empty() -> None:
    assert parse_rooms([], []) == {}


# --- AmpioClient.fetch_rooms() MQTT orchestration -------------------------


async def test_fetch_rooms_raises_when_not_connected() -> None:
    client = AmpioClient("host", username="u", password="p")
    with pytest.raises(AmpioConnectionError):
        await client.fetch_rooms()


async def test_fetch_rooms_publishes_keywords_and_joins_responses(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected

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

    delivery = deliver_later(
        client,
        ("ampio/fromDB/u/data/groups", groups_payload),
        ("ampio/fromDB/u/data/group_devices", group_devices_payload),
    )
    try:
        result = await client.fetch_rooms(timeout=1.0)
    finally:
        await delivery

    assert result == {31: "Salon", 28: "Jadalnia"}
    assert broker.published == [
        ("ampio/control/u/data", b"groups"),
        ("ampio/control/u/data", b"group_devices"),
    ]


async def test_fetch_rooms_times_out_when_response_missing(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, _ = connected
    # Deliver only one of the two responses - groups arrives, group_devices never does.
    delivery = deliver_later(
        client, ("ampio/fromDB/u/data/groups", json.dumps({"List": []}))
    )
    try:
        with pytest.raises(AmpioTimeoutError):
            await client.fetch_rooms(timeout=0.1)
    finally:
        await delivery


async def test_fetch_rooms_treats_malformed_response_as_no_response(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A corrupt reply must end in the retryable timeout, not a fake-valid
    empty map - the same failure contract as a reply that never arrives.
    The raw bytes are still retained for diagnostics."""
    client, _ = connected

    delivery = deliver_later(
        client,
        ("ampio/fromDB/u/data/groups", "not-json"),
        ("ampio/fromDB/u/data/group_devices", "[]"),
    )
    try:
        with pytest.raises(AmpioTimeoutError):
            await client.fetch_rooms(timeout=0.1)
    finally:
        await delivery
    assert client.last_payloads["groups"] == "not-json"


async def test_concurrent_fetch_does_not_steal_the_first_callers_reply(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """Caller B entering between the replies landing and caller A's wakeup
    must not consume A's replies: each caller correlates through its own
    future, so A's map arrives intact."""
    client, _ = connected
    groups = json.dumps({"List": [{"id": 8, "opis_menu": "Salon"}]})
    group_devices = json.dumps({"List": [{"id_grupy": 8, "id_obiektu": 31}]})

    task_a = asyncio.create_task(client.fetch_rooms(timeout=1.0))
    await asyncio.sleep(0)  # A publishes its requests and starts waiting
    # B is queued to run after the replies below are dispatched but before
    # A's wakeup callback - exactly the defective interleaving.
    task_b = asyncio.create_task(client.fetch_rooms(timeout=1.0))
    feed(client, "ampio/fromDB/u/data/groups", groups)
    feed(client, "ampio/fromDB/u/data/group_devices", group_devices)
    assert await task_a == {31: "Salon"}
    # B asked after the first replies were dispatched, so the next pair
    # answers it.
    feed(client, "ampio/fromDB/u/data/groups", groups)
    feed(client, "ampio/fromDB/u/data/group_devices", group_devices)
    assert await task_b == {31: "Salon"}


async def test_concurrent_callers_of_the_same_endpoints_share_one_reply(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """Every concurrent caller of the same endpoint receives the same reply:
    with both callers' waiters registered before anything lands, one reply
    pair resolves both."""
    client, _ = connected
    groups = json.dumps({"List": [{"id": 8, "opis_menu": "Salon"}]})
    group_devices = json.dumps({"List": [{"id_grupy": 8, "id_obiektu": 31}]})

    task_a = asyncio.create_task(client.fetch_rooms(timeout=1.0))
    await asyncio.sleep(0)  # A publishes its requests and registers waiters
    task_b = asyncio.create_task(client.fetch_rooms(timeout=1.0))
    await asyncio.sleep(0)  # B's waiters are registered too
    feed(client, "ampio/fromDB/u/data/groups", groups)
    feed(client, "ampio/fromDB/u/data/group_devices", group_devices)
    assert await task_a == {31: "Salon"}
    assert await task_b == {31: "Salon"}


async def test_late_reply_after_timeout_resolves_nothing_stale(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A timed-out fetch leaves no waiter behind; the late reply is a no-op
    and a fresh call still works."""
    client, _ = connected
    groups = json.dumps({"List": [{"id": 1, "opis_menu": "A"}]})
    group_devices = json.dumps({"List": [{"id_grupy": 1, "id_obiektu": 10}]})

    with pytest.raises(AmpioTimeoutError):
        await client.fetch_rooms(timeout=0.05)

    # The replies to the timed-out request arrive now - nobody is waiting.
    feed(client, "ampio/fromDB/u/data/groups", groups)
    feed(client, "ampio/fromDB/u/data/group_devices", group_devices)

    delivery = deliver_later(
        client,
        ("ampio/fromDB/u/data/groups", groups),
        ("ampio/fromDB/u/data/group_devices", group_devices),
    )
    try:
        assert await client.fetch_rooms(timeout=1.0) == {10: "A"}
    finally:
        await delivery


async def test_fetch_rooms_clears_state_between_calls(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A second call must not see stale events from the first."""
    client, _ = connected

    first = deliver_later(
        client,
        (
            "ampio/fromDB/u/data/groups",
            json.dumps({"List": [{"id": 1, "opis_menu": "A"}]}),
        ),
        (
            "ampio/fromDB/u/data/group_devices",
            json.dumps({"List": [{"id_grupy": 1, "id_obiektu": 10}]}),
        ),
    )
    r1 = await client.fetch_rooms(timeout=1.0)
    await first
    assert r1 == {10: "A"}

    second = deliver_later(
        client,
        (
            "ampio/fromDB/u/data/groups",
            json.dumps({"List": [{"id": 2, "opis_menu": "B"}]}),
        ),
        (
            "ampio/fromDB/u/data/group_devices",
            json.dumps({"List": [{"id_grupy": 2, "id_obiektu": 20}]}),
        ),
    )
    r2 = await client.fetch_rooms(timeout=1.0)
    await second
    assert r2 == {20: "B"}


async def test_fetch_rooms_wraps_publish_failure(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A broker failure during the request publish surfaces as the library's
    own error type, with the aiomqtt original preserved as the cause."""
    client, broker = connected
    broker.publish_errors = [aiomqtt.MqttError("publish failed")]

    with pytest.raises(AmpioConnectionError) as excinfo:
        await client.fetch_rooms(timeout=1.0)
    assert isinstance(excinfo.value.__cause__, aiomqtt.MqttError)


async def test_fetch_timeout_bounds_the_publish_leg(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A broker slow to PUBACK must not stretch the fetch beyond its
    budget: the publishes sit inside the timeout window, so the two
    stalled requests alone (0.4 s here) cannot precede the reply wait."""
    client, broker = connected
    broker.publish_delay = 0.2
    start = asyncio.get_running_loop().time()
    with pytest.raises(AmpioTimeoutError):
        await client.fetch_rooms(timeout=0.1)
    assert asyncio.get_running_loop().time() - start < 0.35
