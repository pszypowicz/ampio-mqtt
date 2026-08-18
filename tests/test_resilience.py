"""The connection must survive bad input and bad consumers.

Each test here stands for a way the client used to die silently: once the
runner task ends, nothing reconnects and every entity is frozen for the
lifetime of the process, so these are about staying alive rather than about
any single message.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import USER, details, feed

from ampio_mqtt import AmpioClient, BusEvent, ModuleUpdated, ObjectUpdated


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


@pytest.mark.parametrize(
    ("of", "topic", "payload"),
    [
        (ModuleUpdated, "ampio/from/CAFE/b/4F", b'{"d":[254,79,60,0]}'),
        (BusEvent, "ampio/from/1/event", b"189"),
    ],
)
def test_every_listener_kind_is_isolated(of: type, topic: str, payload: bytes) -> None:
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devices",
        json.dumps({"List": [{"id": 7, "mac": 0xCAFE, "typ_urzadzenia": 11}]}).encode(),
    )
    client.subscribe(lambda _: (_ for _ in ()).throw(ValueError("boom")), of=of)

    feed(client, topic, payload)  # must not raise


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


def test_backoff_survives_a_long_outage() -> None:
    """Attempts are unbounded, so the exponent must not overflow the float."""
    client = AmpioClient("host", username=USER, reconnect_interval=5.0)
    for attempt in (0, 10, 1024, 100_000):
        assert 0.0 <= client._connection._backoff_seconds(attempt) <= 65.0


# --- reconnect resync -------------------------------------------------------


def _snapshot(oid: int, value: str, on_ms: int) -> bytes:
    stan = json.dumps({"state": value, "on": on_ms})
    return json.dumps({"List": [{"id": oid, "stan_json": stan}]}).encode()


def test_a_newer_snapshot_corrects_a_value_that_changed_during_an_outage() -> None:
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state":"255","on":1786700100000}',
    )
    assert client.objects[41].value == "255"

    # Reconnect: the object was switched off while the connection was down.
    feed(client, f"ampio/fromDB/{USER}/data/states", _snapshot(41, "0", 1786700900000))

    assert client.objects[41].value == "0"


def test_an_older_snapshot_loses_to_the_live_push_that_beat_it() -> None:
    """On a fresh connection the snapshot can arrive after a newer push."""
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state":"fresh","on":1786700900000}',
    )
    feed(
        client,
        f"ampio/fromDB/{USER}/data/states",
        _snapshot(41, "stale", 1786700100000),
    )
    assert client.objects[41].value == "fresh"


def test_an_undated_snapshot_only_fills_a_gap() -> None:
    client = _client()
    feed(client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"live"}')
    feed(
        client,
        f"ampio/fromDB/{USER}/data/states",
        json.dumps({"List": [{"id": 41, "stan_json": '{"state":"undated"}'}]}).encode(),
    )
    assert client.objects[41].value == "live"


def test_snapshot_also_corrects_tilt() -> None:
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devicesDetails",
        details({"id": 66, "typ_komponentu": "roleta_lamelki", "interpretacja": 1}),
    )
    feed(
        client,
        f"ampio/fromDB/{USER}/ob/66/state",
        b'{"state":"95","lammel":"10","on":1786700100000}',
    )
    feed(
        client,
        f"ampio/fromDB/{USER}/data/states",
        json.dumps(
            {
                "List": [
                    {
                        "id": 66,
                        "stan_json": json.dumps(
                            {"state": "100", "lammel": "90", "on": 1786700900000}
                        ),
                    }
                ]
            }
        ).encode(),
    )
    obj = client.objects[66]
    assert (obj.value, obj.tilt_position) == ("100", 90)


# --- params ownership -------------------------------------------------------


def test_a_reply_without_params_keeps_what_params_devices_supplied() -> None:
    """The hidden flag must survive a catalogue that carries no params column."""
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/data/params_devices",
        json.dumps({"List": [{"id": 7, "params": 17}]}).encode(),
    )
    feed(
        client,
        f"ampio/fromDB/{USER}/data/devices",
        details({"id": 7, "typ_komponentu": "lin_wej", "leafId": "0_x_1"}),
    )
    assert client.objects[7].hidden is True

    feed(
        client,
        f"ampio/fromDB/{USER}/config/devicesDetails",
        details({"id": 7, "typ_komponentu": "lin_wej", "leafId": "0_x_1"}),
    )

    obj = client.objects[7]
    assert obj.hidden is True
    assert obj.visible is False


def test_params_present_in_a_reply_still_win() -> None:
    """Un-hiding an object in Designer must reach the consumer."""
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/data/params_devices",
        json.dumps({"List": [{"id": 7, "params": 17}]}).encode(),
    )
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devicesDetails",
        details({"id": 7, "typ_komponentu": "lin_wej", "leafId": "0_x_1", "params": 1}),
    )
    assert client.objects[7].hidden is False


def test_updated_at_tracks_the_report_a_value_came_from() -> None:
    client = _client()
    feed(
        client, f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"1","on":1786700100000}'
    )
    assert client.objects[41].updated_at == 1786700100.0
