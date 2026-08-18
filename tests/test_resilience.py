"""The connection must survive bad input and bad consumers.

Each test here stands for a way the client used to die silently: once the
runner task ends, nothing reconnects and every entity is frozen for the
lifetime of the process, so these are about staying alive rather than about
any single message.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import USER, feed

from ampio_mqtt import AmpioClient, ObjectUpdated


def _client() -> AmpioClient:
    return AmpioClient("host", username=USER)


# --- listeners are consumer code and may raise ------------------------------


def test_a_raising_listener_does_not_stop_the_others() -> None:
    client = _client()
    seen: list[int] = []
    client.subscribe(lambda e: (_ for _ in ()).throw(ValueError("boom")))
    client.subscribe(lambda e: seen.append(e.object.id), of=ObjectUpdated)

    feed(client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"1"}')

    assert seen == [41]


def test_a_raising_listener_does_not_stop_later_messages() -> None:
    client = _client()
    client.subscribe(lambda e: (_ for _ in ()).throw(ValueError("boom")))

    feed(client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"1"}')
    feed(client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"2"}')

    assert client.objects[41].value == "2"


# --- replies of the wrong shape --------------------------------------------


@pytest.mark.parametrize(
    "surface",
    [
        "config/devicesDetails",
        "config/devices",
        "data/devices",
        "data/params_devices",
        "data/states",
        "data/scenes",
    ],
)
@pytest.mark.parametrize("payload", [b"null", b"[]", b'{"List": 5}', b'{"List": [1]}'])
def test_malformed_replies_never_escape_the_dispatcher(
    surface: str, payload: bytes
) -> None:
    client = _client()
    feed(client, f"ampio/fromDB/{USER}/{surface}", payload)  # must not raise


def test_a_malformed_reply_does_not_stop_later_messages() -> None:
    client = _client()
    feed(client, f"ampio/fromDB/{USER}/config/devicesDetails", b"null")
    feed(client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"77"}')
    assert client.objects[41].value == "77"


@pytest.mark.parametrize(
    "payload",
    [b'{"d":[254,79,null,0]}', b'{"d":[254,79,"x",0]}', b'{"d":"nope"}', b"null"],
)
def test_malformed_diagnostics_frames_are_ignored(payload: bytes) -> None:
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devices",
        b'{"List":[{"id":7,"mac":51966}]}',
    )
    feed(client, "ampio/from/CAFE/b/4F", payload)  # must not raise
    assert client.modules[7].supply_voltage is None


# --- lifecycle --------------------------------------------------------------


async def test_stop_reports_a_failed_runner_instead_of_raising() -> None:
    client = _client()

    async def explode() -> None:
        raise RuntimeError("connection loop died")

    client._connection._runner = asyncio.create_task(explode())
    await asyncio.sleep(0)

    await client.stop()  # must not raise
    assert client._connection._runner is None


async def test_stop_is_idempotent() -> None:
    client = _client()
    await client.stop()
    await client.stop()


# Attempts are unbounded, so the exponent must be clamped: a broker down
# overnight would otherwise overflow the float and kill the retry loop.
@pytest.mark.parametrize("attempt", [0, 1, 2, 5, 6, 7, 16, 100, 100_000])
def test_backoff_is_capped_exponential_with_bounded_jitter(attempt: int) -> None:
    base = 5.0
    client = AmpioClient("host", username=USER, reconnect_interval=base)
    capped = min(60.0, base * 2 ** min(attempt, 16))
    assert capped <= client._connection._backoff_seconds(attempt) <= capped + base


def test_updated_at_tracks_the_report_a_value_came_from() -> None:
    client = _client()
    feed(
        client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"1","on":1786700100000}'
    )
    assert client.objects[41].updated_at == 1786700100.0
