"""Tests for the scene catalogue and scene commands."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import API_TOPIC, USER, FakeBroker, deliver_later

from ampio_mqtt import (
    AmpioClient,
    AmpioConnectionError,
    AmpioProtocolError,
    AmpioTimeoutError,
)
from ampio_mqtt._protocol import parse_scenes


# One scene row as the live M-SERV serves it. `Infos` carries the structured
# form of each action, and the library reads the object ids out of it.
def _scene(**over: object) -> dict:
    row: dict = {
        "id": 1,
        "parentId": -1,
        "sceneName": "Evening",
        "sceneIdent": "",
        "active": 1,
        "lp": 0,
        "Actions": [{"action": "set/50/setColors/65536", "delay": 0}],
        "Infos": [
            {
                "id": 50,
                "param1": -1,
                "param2": -1,
                "param3": -1,
                "value": 65536,
                "delay": "0",
            }
        ],
        "Schedules": [],
    }
    return {**row, **over}


def _catalogue(*rows: dict) -> str:
    return json.dumps({"List": list(rows)})


_PAYLOAD = _catalogue(
    _scene(),
    _scene(
        id=7,
        parentId=1,
        sceneName="Away",
        active=0,
        Actions=[
            {"action": "set/64/turnOff", "delay": 0},
            {"action": "set/48/setRollerPos/0/101", "delay": 5},
        ],
        Infos=[{"id": 64}, {"id": 48}],
    ),
)


def test_parses_the_catalogue() -> None:
    first, second = parse_scenes(_PAYLOAD)
    assert (first.id, first.scene_name, first.active) == (1, "Evening", True)
    assert first.parent_id is None  # -1 means top level
    assert first.object_ids == frozenset({50})
    assert (second.id, second.active, second.parent_id) == (7, False, 1)
    assert second.object_ids == frozenset({64, 48})


@pytest.mark.parametrize("column", ["id", "parentId", "sceneName", "active", "Infos"])
def test_a_scene_row_without_a_served_column_is_refused(column: str) -> None:
    """Every live row carries these, and the library reads each one."""
    row = _scene()
    del row[column]
    with pytest.raises(AmpioProtocolError, match=column):
        parse_scenes(_catalogue(row))


@pytest.mark.parametrize("infos", [5, "x", None, {"id": 1}, [{"value": 1}], [7]])
def test_a_malformed_infos_annex_is_refused(infos: object) -> None:
    """`Infos` is the structured form of the scene's actions, and its ids are
    what relate a scene to its objects. A shape that carries none of them
    would read as a scene that touches nothing."""
    with pytest.raises(AmpioProtocolError, match="Infos"):
        parse_scenes(_catalogue(_scene(Infos=infos)))


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "null",
        "[]",  # a reply of the wrong shape is not an empty catalogue
        "{}",  # no List key either
        '{"List": [1, "x"]}',  # a row that is not an object is not a scene
    ],
)
def test_a_reply_of_the_wrong_shape_is_refused(payload: str) -> None:
    with pytest.raises(AmpioProtocolError):
        parse_scenes(payload)


def test_an_empty_catalogue_reads_as_no_scenes() -> None:
    assert parse_scenes('{"List": []}') == []


def test_a_non_text_scene_name_is_refused() -> None:
    with pytest.raises(AmpioProtocolError, match="sceneName"):
        parse_scenes(_catalogue(_scene(sceneName=7)))


def test_an_empty_scene_name_reads_through() -> None:
    """The app asks for a name, but an empty one names no value the library
    has to resolve, so it passes through as the empty string."""
    assert parse_scenes(_catalogue(_scene(sceneName="")))[0].scene_name == ""


async def test_fetch_scenes_maps_a_refused_reply_to_the_retryable_error(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A reply the parse refuses resolves no waiter, so the fetch ends in the
    same retryable error as silence rather than leaking a bare exception."""
    client, _broker = connected
    bad = _catalogue(_scene(Infos=5))

    delivery = deliver_later(client, (f"ampio/fromDB/{USER}/data/scenes", bad))
    try:
        with pytest.raises(AmpioTimeoutError):
            await client.fetch_scenes(timeout=0.2)
    finally:
        await delivery


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda c: c.run_scene(1), b"/api/run/scene/1"),
        (lambda c: c.off_scene(1), b"/api/off/scene/1"),
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

    delivery = deliver_later(client, (f"ampio/fromDB/{USER}/data/scenes", _PAYLOAD))
    try:
        scenes = await client.fetch_scenes(timeout=2)
    finally:
        await delivery
    assert [s.scene_name for s in scenes] == ["Evening", "Away"]
    assert broker.published == [(f"ampio/control/{USER}/data", b"scenes")]


async def test_concurrent_fetches_get_distinct_lists(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """One reply resolves every concurrent caller with equal scenes, but
    each caller gets its own list - sorting one must not reorder another."""
    client, _broker = connected
    delivery = deliver_later(client, (f"ampio/fromDB/{USER}/data/scenes", _PAYLOAD))
    try:
        first, second = await asyncio.gather(
            client.fetch_scenes(timeout=2), client.fetch_scenes(timeout=2)
        )
    finally:
        await delivery
    assert first == second
    assert first is not second
