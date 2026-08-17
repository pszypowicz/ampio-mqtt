"""The store applies messages without a client, a broker, or an event loop.

These drive `AmpioStore` directly, which is the point of it being separate:
protocol behaviour is reachable from a plain function call, and what a message
changed is a return value rather than something to reconstruct from callbacks.
"""

from __future__ import annotations

import json
import logging

import pytest

from ampio_mqtt._store import AmpioStore
from ampio_mqtt.models import AmpioObject

USER = "u"

DEVICES_TOPIC = f"ampio/fromDB/{USER}/config/devices"
DETAILS_TOPIC = f"ampio/fromDB/{USER}/config/devicesDetails"


def _store() -> AmpioStore:
    return AmpioStore(USER)


def _devices(*macs: int) -> str:
    return json.dumps(
        {
            "List": [
                {
                    "id": i,
                    "mac": mac,
                    "mac_global": 100 + i,
                    "nazwa_urzadzenia": chr(ord("A") + i - 1),
                    "typ_urzadzenia": 11,
                }
                for i, mac in enumerate(macs, start=1)
            ]
        }
    )


def _flaga_details(*object_module_pairs: tuple[int, int]) -> str:
    return json.dumps(
        {
            "List": [
                {
                    "id": oid,
                    "id_urzadzenia": dev,
                    "typ_komponentu": "flaga",
                    "interpretacja": 1,
                    "funkcja": 3,
                    "opis_menu": f"flag-{oid}",
                }
                for oid, dev in object_module_pairs
            ]
        }
    )


def _catalogue(**overrides: object) -> str:
    row = {
        "id": 41,
        "typ_komponentu": "przekaznik",
        "interpretacja": 1,
        "leafId": "0_a_1",
        "opis_menu": "Lamp",
        "params": 1,
    }
    row.update(overrides)
    return json.dumps({"List": [row]})


def test_a_catalogue_reply_reports_its_endpoint_and_the_rows_it_changed() -> None:
    applied = _store().apply(f"ampio/fromDB/{USER}/config/devicesDetails", _catalogue())
    assert applied.endpoint is not None and applied.endpoint.name == "details"
    assert applied.parsed is True
    assert [o.id for o in applied.objects] == [41]


def test_an_unchanged_row_reports_nothing() -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    store.apply(topic, _catalogue())
    assert store.apply(topic, _catalogue()).objects == []


def test_a_changed_row_reports_only_that_row() -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    store.apply(topic, _catalogue())
    applied = store.apply(topic, _catalogue(opis_menu="Renamed"))
    assert [o.name for o in applied.objects] == ["Renamed"]


def test_an_unreadable_reply_reports_its_endpoint_but_not_parsed() -> None:
    """The caller uses this to keep discovery from latching on a bad payload."""
    applied = _store().apply(f"ampio/fromDB/{USER}/config/devicesDetails", "null")
    assert applied.endpoint is not None
    assert applied.parsed is False
    assert applied.objects == []


def test_a_reply_with_no_state_handler_still_counts_as_parsed() -> None:
    applied = _store().apply(f"ampio/fromDB/{USER}/data/groups", '{"List": []}')
    assert applied.endpoint is not None and applied.endpoint.name == "groups"
    assert applied.parsed is True


@pytest.mark.parametrize(
    ("topic", "payload", "attr"),
    [
        (f"ampio/fromDB/{USER}/ob/41/state", '{"state":"1"}', "objects"),
        ("ampio/from/1/event", "189", "events"),
    ],
)
def test_live_messages_carry_no_endpoint(topic: str, payload: str, attr: str) -> None:
    applied = _store().apply(topic, payload)
    assert applied.endpoint is None
    assert getattr(applied, attr)


def test_an_unrelated_topic_changes_nothing() -> None:
    applied = _store().apply("totally/unrelated", "anything")
    assert (applied.endpoint, applied.objects, applied.modules, applied.events) == (
        None,
        [],
        [],
        [],
    )


def test_diagnostics_report_the_module_they_touched() -> None:
    store = _store()
    store.apply(
        f"ampio/fromDB/{USER}/config/devices",
        json.dumps({"List": [{"id": 7, "mac": 0xCAFE, "typ_urzadzenia": 11}]}),
    )
    applied = store.apply("ampio/from/CAFE/b/4F", '{"d":[254,79,63,142]}')
    assert [m.id for m in applied.modules] == [7]
    assert store.modules[7].supply_voltage == 12.6


def test_an_object_leaving_the_index_is_freed_from_raw_suppression() -> None:
    """An id recycled onto a type the raw tree does not carry must not freeze.

    DB ids are reassigned when a module is replaced, so a raw-proven flag can
    come back as something else entirely - which no raw channel feeds.
    """
    store = _store()
    store.apply(
        f"ampio/fromDB/{USER}/config/devices",
        json.dumps({"List": [{"id": 7, "mac": 0xCAFE, "typ_urzadzenia": 11}]}),
    )
    store.apply(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        json.dumps(
            {
                "List": [
                    {
                        "id": 50,
                        "id_urzadzenia": 7,
                        "typ_komponentu": "flaga",
                        "interpretacja": 1,
                        "funkcja": 32,
                    }
                ]
            }
        ),
    )
    store.apply("ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].value == "1"

    # After a module swap the id comes back as a cover, which no raw channel
    # feeds, so its only updates are the per-object ones.
    store.apply(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        json.dumps(
            {
                "List": [
                    {
                        "id": 50,
                        "id_urzadzenia": 7,
                        "typ_komponentu": "roleta_procenty",
                        "interpretacja": 1,
                        "funkcja": 2,
                    }
                ]
            }
        ),
    )
    applied = store.apply(f"ampio/fromDB/{USER}/ob/50/state", '{"state":"55"}')

    assert [o.id for o in applied.objects] == [50]
    assert store.objects[50].value == "55"


def test_the_store_is_the_only_thing_holding_state() -> None:
    store = _store()
    store.apply(f"ampio/fromDB/{USER}/data/info", '{"Results": {"mac": "47846"}}')
    assert store.server_info is not None and store.server_info.mac == 47846
    assert isinstance(store.objects, dict)
    assert all(isinstance(o, AmpioObject) for o in store.objects.values())


def test_colliding_macs_keep_both_modules_and_warn_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _store()
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        store.apply(DEVICES_TOPIC, _devices(7, 7))
    assert store.colliding_macs == {7}
    assert sorted(store.modules) == [1, 2]
    warnings = [r for r in caplog.records if "colliding module macs" in r.getMessage()]
    assert len(warnings) == 1
    assert "module 1 (A)" in warnings[0].getMessage()
    assert "module 2 (B)" in warnings[0].getMessage()

    # A reconnect re-requests the catalogue; a standing collision is old news.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        store.apply(DEVICES_TOPIC, _devices(7, 7))
    assert caplog.records == []
    assert store.colliding_macs == {7}


def test_a_resolved_collision_clears_the_set() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(7, 7))
    store.apply(DEVICES_TOPIC, _devices(7, 8))
    assert store.colliding_macs == frozenset()


def test_unique_macs_produce_no_collision_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _store()
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        store.apply(DEVICES_TOPIC, _devices(7, 8))
    assert store.colliding_macs == frozenset()
    assert caplog.records == []


def test_a_colliding_mac_routes_no_diagnostics() -> None:
    """The sender is unknowable, and a wrong attribution would stand silently."""
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(7, 7))
    applied = store.apply("ampio/from/7/b/4F", '{"d":[254,79,63,142]}')
    assert applied.modules == []
    assert all(m.supply_voltage is None for m in store.modules.values())


def test_a_colliding_mac_routes_no_raw_edges_but_per_object_still_updates() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(7, 7))
    store.apply(DETAILS_TOPIC, _flaga_details((301, 1), (302, 2)))

    applied = store.apply("ampio/from/7/state/f/3", "1")
    assert applied.objects == []
    assert store.objects[301].value is None
    assert store.objects[302].value is None

    applied = store.apply(f"ampio/fromDB/{USER}/ob/301/state", '{"state":"1"}')
    assert [o.id for o in applied.objects] == [301]
    assert store.objects[301].value == "1"
