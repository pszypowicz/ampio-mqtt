"""The connection must survive bad input and bad consumers.

Each test here stands for a way the client used to die silently: once the
runner task ends, nothing reconnects and every entity is frozen for the
lifetime of the process, so these are about staying alive rather than about
any single message.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from conftest import USER, FakeBroker, feed, make_client

from ampio_mqtt import AmpioClient, ConnectionDied, ObjectUpdated


def _client() -> AmpioClient:
    return AmpioClient("host", username=USER)


def _establish(client: AmpioClient, *oids: int) -> None:
    """Catalogue rows establishing the objects the live pushes then update."""
    feed(
        client,
        f"ampio/fromDB/{USER}/data/devices",
        json.dumps({"List": [{"id": oid} for oid in oids]}),
    )


# --- listeners are consumer code and may raise ------------------------------


def test_a_raising_listener_does_not_stop_the_others() -> None:
    client = _client()
    _establish(client, 41)
    seen: list[int] = []
    client.subscribe(lambda e: (_ for _ in ()).throw(ValueError("boom")))
    client.subscribe(lambda e: seen.append(e.object.id), of=ObjectUpdated)

    feed(client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"1"}')

    assert seen == [41]


def test_a_raising_listener_does_not_stop_later_messages() -> None:
    client = _client()
    _establish(client, 41)
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
    _establish(client, 41)
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


async def test_stop_after_the_loop_died_does_not_raise() -> None:
    broker = FakeBroker()
    broker.stream_error = RuntimeError("injected bug")
    client = make_client(broker)
    died: list[ConnectionDied] = []
    client.subscribe(died.append, of=ConnectionDied)
    await client.start(timeout=2.0, discovery_timeout=0.05)
    async with asyncio.timeout(2.0):
        while not died:
            await asyncio.sleep(0.01)
    await client.stop()  # must not raise
    assert client.available is False


async def test_stop_is_idempotent() -> None:
    client = _client()
    await client.stop()
    await client.stop()


# Attempts are unbounded, so the exponent must be clamped: a broker down
# overnight would otherwise overflow the float and kill the retry loop.
@pytest.mark.parametrize("attempt", [0, 16, 100_000])
def test_backoff_stays_finite_and_capped(attempt: int) -> None:
    base = 5.0
    client = AmpioClient("host", username=USER, reconnect_interval=base)
    backoff = client._connection._backoff_seconds(attempt)
    assert base <= backoff <= 60.0 + base


def test_updated_at_tracks_the_report_a_value_came_from() -> None:
    client = _client()
    _establish(client, 41)
    feed(
        client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"1","on":1786700100000}'
    )
    assert client.objects[41].updated_at == 1786700100.0


# --- a message-processing bug costs one message, not the connection ---------


async def test_poison_message_does_not_kill_the_connection(
    connected: tuple[AmpioClient, FakeBroker],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The store's parsers make this unreachable today; the fragile shim
    stands in for a future defect. The guard's contract: the failing
    payload is dropped with one logged traceback, the connection stays
    up, and later messages process normally."""
    client, _broker = connected
    _establish(client, 5)
    original = client._store.apply

    def fragile(topic: str, payload: str) -> object:
        if payload == "POISON":
            raise RuntimeError("simulated processing defect")
        return original(topic, payload)

    client._store.apply = fragile  # type: ignore[method-assign]
    topic = f"ampio/fromDB/{USER}/ob/5/state"
    with caplog.at_level(logging.ERROR):
        feed(client, topic, b"POISON")  # must not raise
    assert client.available
    assert sum("failed processing" in r.message for r in caplog.records) == 1

    feed(client, topic, b'{"state":"42"}')
    assert client.objects[5].value == "42"

    # A recurring poison on the same topic stays out of the error log -
    # the traceback was already recorded once.
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        feed(client, topic, b"POISON")
    assert not caplog.records
