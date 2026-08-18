"""Tests for the scene catalogue and scene commands."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import API_TOPIC, USER, FakeBroker, feed

from ampio_mqtt import AmpioClient, AmpioConnectionError
from ampio_mqtt._protocol import parse_scenes

_PAYLOAD = json.dumps(
    {
        "List": [
            {
                "id": 1,
                "parentId": -1,
                "sceneName": "Schody noc",
                "active": 1,
                "Actions": [{"action": "set/50/setColors/65536", "delay": 0}],
                "Infos": [{"id": 50, "value": 65536, "delay": "0"}],
                "Schedules": [],
            },
            {
                "id": 7,
                "parentId": 1,
                "sceneName": "Wyjście",
                "active": 0,
                "Actions": [
                    {"action": "set/64/turnOff", "delay": 0},
                    {"action": "set/48/setRollerPos/0/101", "delay": 5},
                ],
                "Infos": [{"id": 64}, {"id": 48}],
            },
            # No "active" column: the baseline server always sends it, so
            # this is the shape-drift case - it must
            # read enabled, matching the dataclass default.
            {
                "id": 9,
                "parentId": -1,
                "sceneName": "Bez kolumny",
                "Infos": [],
            },
        ]
    }
)


def test_parses_the_catalogue() -> None:
    scenes = parse_scenes(_PAYLOAD)
    assert scenes is not None
    first, second, third = scenes
    assert (first.id, first.name, first.active) == (1, "Schody noc", True)
    assert first.parent_id is None  # -1 means top level
    assert first.object_ids == frozenset({50})
    assert (second.id, second.active, second.parent_id) == (7, False, 1)
    assert second.object_ids == frozenset({64, 48})
    assert (third.id, third.active) == (9, True)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not json", None),
        ("null", None),
        ("[]", None),  # a reply of the wrong shape is not an empty catalogue
        ("{}", None),  # no List key either
        ('{"List": []}', []),  # this is what an empty catalogue looks like
        ('{"List": [1, "x"]}', []),  # rows that are not objects are skipped
    ],
)
def test_unparseable_or_empty_payloads(payload: str, expected: list | None) -> None:
    assert parse_scenes(payload) == expected


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda c: c.run_scene(1), b"/api/run/scene/1"),
        (lambda c: c.turn_scene_off(1), b"/api/off/scene/1"),
        (lambda c: c.undo_scene(1), b"/api/undo/scene/1"),
    ],
)
async def test_scene_commands(
    connected: tuple[AmpioClient, FakeBroker], call, expected: bytes
) -> None:
    client, broker = connected
    await call(client)
    assert broker.published == [(API_TOPIC, expected)]


async def test_scene_commands_require_a_connection() -> None:
    client = AmpioClient("host", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.run_scene(1)


async def test_fetch_scenes_requests_and_parses_the_reply(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected

    async def _deliver() -> None:
        # Give fetch_scenes a turn to publish before the reply arrives.
        await asyncio.sleep(0)
        feed(client, f"ampio/fromDB/{USER}/data/scenes", _PAYLOAD)

    scenes, _ = await asyncio.gather(client.fetch_scenes(timeout=2), _deliver())
    assert [s.name for s in scenes] == ["Schody noc", "Wyjście", "Bez kolumny"]
    assert broker.published == [(f"ampio/control/{USER}/data", b"scenes")]
