"""The store applies messages without a client, a broker, or an event loop.

These drive `AmpioStore` directly, which is the point of it being separate:
protocol behaviour is reachable from a plain function call, and what a message
changed is a return value rather than something to reconstruct from callbacks.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import fields

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


# A module whose mac is 0xCAFE, so its raw topics are `ampio/from/CAFE/...`.
_PANEL = {"id": 7, "mac": 0xCAFE, "typ_urzadzenia": 11, "nazwa_urzadzenia": "panel"}


def _flaga_row(oid: int, funkcja: int, dev: int = 7) -> dict:
    return {
        "id": oid,
        "id_urzadzenia": dev,
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "funkcja": funkcja,
        "opis_menu": "Flag",
    }


def _details_rows(*items) -> str:
    return json.dumps({"Status": 0, "List": list(items)})


def _devices_rows(*items) -> str:
    return json.dumps({"List": list(items)})


def _states_rows(*items) -> str:
    return json.dumps({"List": list(items)})


def _info(**fields_) -> str:
    return json.dumps({"Results": fields_})


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


def test_a_below_baseline_server_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    payload = '{"Results": {"mac": 1, "serverVersion": "409"}}'
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        store.apply(topic, payload)
        # The re-request every reconnect issues repeats the same version.
        store.apply(topic, payload)
    warnings = [r for r in caplog.records if "baseline" in r.getMessage()]
    assert len(warnings) == 1
    assert "409" in warnings[0].getMessage()


def test_a_baseline_server_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    store = _store()
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        store.apply(
            f"ampio/fromDB/{USER}/data/info",
            '{"Results": {"mac": 1, "serverVersion": "1865"}}',
        )
    assert caplog.records == []


def test_on_demand_reply_parseability_gates_parsed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Endpoints with no mutating handler still report whether their reply
    parsed, so a corrupt reply cannot complete a fetch (it is List-shaped
    or it does not count)."""
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/groups"
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        bad = store.apply(topic, "{not json !!")
    assert bad.endpoint is not None and bad.endpoint.name == "groups"
    assert bad.parsed is False
    assert any("groups" in r.getMessage() for r in caplog.records)

    not_a_list_reply = store.apply(topic, "[]")
    assert not_a_list_reply.parsed is False

    good = store.apply(topic, '{"List": []}')
    assert good.parsed is True


def _raw_proven_flag(store: AmpioStore, mac: int = 0xCAFE) -> None:
    """Discover one flaga (ob/10 on module 1, channel f/3) and land a raw edge."""
    store.apply(DEVICES_TOPIC, _devices(mac))
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    store.apply(f"ampio/from/{mac:X}/state/f/3", "1")


def _snapshot(state: str, on_ms: int | None) -> str:
    stan: dict[str, object] = {"state": state}
    if on_ms is not None:
        stan["on"] = on_ms
    return json.dumps({"List": [{"id": 10, "stan_json": json.dumps(stan)}]})


STATES_TOPIC = f"ampio/fromDB/{USER}/data/states"


def test_server_dated_snapshot_never_regresses_a_local_dated_raw_edge() -> None:
    """The raw tree is undated, so a raw edge is stamped with the local
    clock. A snapshot's server date is incomparable to that - on an unsynced
    M-SERV the skew is unbounded - so it must never decide against the edge,
    however far in the future it reads."""
    store = _store()
    _raw_proven_flag(store)
    far_future = int((time.time() + 7200) * 1000)
    applied = store.apply(STATES_TOPIC, _snapshot("0", far_future))
    assert applied.objects == []
    assert store.objects[10].value == "1"


def test_echo_anchors_a_raw_edge_to_the_server_clock() -> None:
    """The per-object echo of a raw edge carries the server `on` date. It is
    dropped as a notification (the raw value is authoritative) but its date
    re-anchors the object, after which snapshot supersession is a same-clock
    comparison: older server dates lose, newer ones win - resync intact."""
    store = _store()
    _raw_proven_flag(store)
    echo_on = int((time.time() + 7200) * 1000)  # skewed server clock
    applied = store.apply(
        f"ampio/fromDB/{USER}/ob/10/state",
        json.dumps({"state": "255", "on": echo_on}),
    )
    assert applied.objects == []  # no re-notify
    obj = store.objects[10]
    assert obj.value == "1"  # raw form kept
    assert obj.updated_at == echo_on / 1000.0  # anchored

    stale = store.apply(STATES_TOPIC, _snapshot("0", echo_on - 10_000))
    assert stale.objects == [] and obj.value == "1"

    resync = store.apply(STATES_TOPIC, _snapshot("0", echo_on + 10_000))
    assert [o.id for o in resync.objects] == [10]
    assert obj.value == "0"


def test_a_dated_snapshot_beats_an_undated_seed() -> None:
    store = _store()
    store.apply(STATES_TOPIC, _snapshot("5", None))
    assert store.objects[10].value == "5"
    assert store.objects[10].updated_at is None
    applied = store.apply(STATES_TOPIC, _snapshot("7", 1779560000000))
    assert [o.id for o in applied.objects] == [10]
    assert store.objects[10].value == "7"


def test_echo_of_an_earlier_edge_does_not_disturb_a_fast_toggle() -> None:
    """Edge 1, edge 2, then the echo of edge 1: the value must stay edge 2's
    and nothing may notify - the echo contributes only its timestamp."""
    store = _store()
    _raw_proven_flag(store)
    store.apply(f"ampio/from/{0xCAFE:X}/state/f/3", "0")  # edge 2
    echo_on = int(time.time() * 1000)
    applied = store.apply(
        f"ampio/fromDB/{USER}/ob/10/state",
        json.dumps({"state": "255", "on": echo_on}),  # echo of edge 1
    )
    assert applied.objects == []
    assert store.objects[10].value == "0"
    assert store.objects[10].updated_at == echo_on / 1000.0


def test_the_config_catalogue_evicts_what_it_stopped_listing() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1), (11, 2)))
    assert set(store.objects) == {10, 11}

    applied = store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    assert [o.id for o in applied.removed_objects] == [11]
    assert set(store.objects) == {10}

    # The unchanged catalogue on the next refresh removes nothing further.
    again = store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    assert again.removed_objects == []


def test_the_devices_reply_evicts_missing_modules() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    applied = store.apply(DEVICES_TOPIC, _devices(0xCAFE))
    assert [m.id for m in applied.removed_modules] == [2]
    assert set(store.modules) == {1}


def test_an_evicted_objects_raw_channel_no_longer_routes() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1), (11, 2)))
    store.apply(f"ampio/from/{0xBEEF:X}/state/f/3", "1")
    assert store.objects[11].value == "1"

    store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    applied = store.apply(f"ampio/from/{0xBEEF:X}/state/f/3", "0")
    assert applied.objects == []
    assert 11 not in store.objects


def test_the_app_sync_catalogue_evicts_only_on_the_restricted_tier() -> None:
    data_topic = f"ampio/fromDB/{USER}/data/devices"
    info_topic = f"ampio/fromDB/{USER}/data/info"

    # Admin tier: data/devices is a second view, not the authority.
    store = _store()
    store.apply(info_topic, '{"Results": {"mac": 1, "userId": "-1"}}')
    store.apply(data_topic, _flaga_details((10, 1), (11, 1)))
    applied = store.apply(data_topic, _flaga_details((10, 1)))
    assert applied.removed_objects == []
    assert set(store.objects) == {10, 11}

    # Restricted tier: the grant bounds the store, so the reply is complete
    # for the account and a vanished row is a revocation.
    store = _store()
    store.apply(info_topic, '{"Results": {"mac": 1, "userId": "4"}}')
    store.apply(data_topic, _flaga_details((10, 1), (11, 1)))
    applied = store.apply(data_topic, _flaga_details((10, 1)))
    assert [o.id for o in applied.removed_objects] == [11]
    assert set(store.objects) == {10}


def test_an_empty_catalogue_reply_never_mass_evicts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE))
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        details = store.apply(DETAILS_TOPIC, json.dumps({"Status": 0, "List": []}))
        devices = store.apply(DEVICES_TOPIC, json.dumps({"List": []}))
    assert details.removed_objects == [] and devices.removed_modules == []
    assert set(store.objects) == {10} and set(store.modules) == {1}
    assert sum("refusing to evict" in r.getMessage() for r in caplog.records) == 2


def test_live_messages_touch_last_seen_snapshots_do_not() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE))
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    assert store.modules[1].last_seen is None

    store.apply(STATES_TOPIC, _snapshot("1", 1779560000000))
    assert store.modules[1].last_seen is None

    before = time.time()
    store.apply(f"ampio/from/{0xCAFE:X}/state/f/3", "1")
    seen = store.modules[1].last_seen
    assert seen is not None and before <= seen <= time.time()


# --- catalogues, state pushes, and snapshots --------------------------------


def test_details_populate_and_classify() -> None:
    store = _store()
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {
                "id": 41,
                "id_urzadzenia": 3,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "Salon",
            },
            {
                "id": 107,
                "id_urzadzenia": 3,
                "typ_komponentu": "lin_wej",
                "interpretacja": 7,
                "opis_menu": "CO2",
            },
            {
                "id": 1,
                "id_urzadzenia": 1,
                "typ_komponentu": "przekaznik",
                "interpretacja": 1,
                "opis_menu": "Pump",
            },
        ),
    )

    assert set(store.objects) == {41, 107, 1}
    temp = store.objects[41]
    assert temp.kind is not None and temp.kind.device_class == "temperature"
    assert temp.name == "Salon" and temp.device_id == 3
    # The raw `interpretacja` selector is retained on the object for consumers,
    # alongside the resolved `kind` the library derives from it.
    assert store.objects[107].interpretacja == 7
    # relay is not a sensor
    assert store.objects[1].is_sensor is False
    assert set(store.state.sensors) == {41, 107}


def test_devices_populate_modules_with_model_and_versions() -> None:
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        _devices_rows(
            {
                "id": 17,
                "mac": 52111,
                "typ_urzadzenia": 44,
                "nazwa_urzadzenia": "m-sens salon",
                "wersja_softu": 63,
                "wersja_pcb": 7,
            },
            {
                "id": 99,
                "mac": 1,
                "typ_urzadzenia": 999,
                "nazwa_urzadzenia": "Mystery",
                "wersja_softu": 1,
                "wersja_pcb": 2,
            },
        ),
    )

    mod = store.modules[17]
    assert mod.name == "m-sens salon"
    assert mod.type == 44
    assert mod.model == "M-SENS"
    assert mod.sw_version == 63
    assert mod.hw_version == 7
    # Unknown type code -> no model name, but the module is still tracked.
    assert store.modules[99].model is None


def test_state_updates_module_last_seen_with_local_receive_time() -> None:
    """A live push marks the module seen at local receive time - the server's
    `on` date is state provenance, never liveness evidence (one clock only)."""
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        _devices_rows(
            {"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}
        ),
    )
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {
                "id": 41,
                "id_urzadzenia": 17,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
            }
        ),
    )
    assert store.modules[17].last_seen is None

    before = time.time()
    store.apply(
        f"ampio/fromDB/{USER}/ob/41/state",
        '{"state": "22.5", "on": 1779565263813}',
    )
    first_seen = store.modules[17].last_seen
    assert first_seen is not None and before <= first_seen <= time.time()

    # Another push refreshes it, regardless of its server date being older.
    store.apply(
        f"ampio/fromDB/{USER}/ob/41/state",
        '{"state": "21.0", "on": 1779560000000}',
    )
    later_seen = store.modules[17].last_seen
    assert later_seen is not None and later_seen >= first_seen


def test_state_push_with_numeric_state_is_stored_as_string() -> None:
    """A broker that emits unquoted numbers in `state` still yields str value."""
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        _devices_rows(
            {"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}
        ),
    )
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {
                "id": 41,
                "id_urzadzenia": 17,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
            }
        ),
    )
    store.apply(
        f"ampio/fromDB/{USER}/ob/41/state",
        '{"state": 24.4, "on": 1779560000000}',
    )
    value = store.objects[41].value
    assert value == "24.4"
    assert isinstance(value, str)


def test_states_snapshot_seeds_value_without_touching_last_seen() -> None:
    """The bulk states reply seeds the value but is not liveness evidence:
    it replays DB state that may be arbitrarily old, so last_seen stays
    None until a live message arrives."""
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        _devices_rows(
            {"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}
        ),
    )
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {
                "id": 41,
                "id_urzadzenia": 17,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
            }
        ),
    )
    assert store.objects[41].value is None
    assert store.modules[17].last_seen is None

    store.apply(
        STATES_TOPIC,
        _states_rows(
            {
                "id": 41,
                "stan_json": '{"state": "22.5", "on": 1779560000000}',
                "upTime": 600,
            }
        ),
    )
    assert store.objects[41].value == "22.5"
    assert store.objects[41].updated_at == 1779560000.0
    assert store.modules[17].last_seen is None


def test_states_snapshot_does_not_overwrite_live_value() -> None:
    """A snapshot does not regress a value already set by a live push."""
    store = _store()
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {"id": 41, "typ_komponentu": "temp", "interpretacja": 1, "opis_menu": "T"}
        ),
    )
    store.apply(
        f"ampio/fromDB/{USER}/ob/41/state",
        '{"state": "fresh", "on": 1779570000000}',
    )
    assert store.objects[41].value == "fresh"

    store.apply(
        STATES_TOPIC,
        _states_rows(
            {"id": 41, "stan_json": '{"state": "stale", "on": 1779560000000}'}
        ),
    )
    assert store.objects[41].value == "fresh"


def test_states_snapshot_creates_placeholder_for_unknown_object() -> None:
    """A state for an object whose metadata is not yet known is still tracked."""
    store = _store()
    store.apply(
        STATES_TOPIC,
        _states_rows({"id": 999, "stan_json": '{"state": "1", "on": 1779560000000}'}),
    )
    assert store.objects[999].value == "1"
    # The kind is the generic fallback because no metadata existed.
    assert store.objects[999].kind is not None
    assert store.objects[999].kind.key == "value"


def test_details_stan_json_seed_does_not_touch_last_seen() -> None:
    """The catalogue's stan_json seed carries state, not liveness - like the
    bulk snapshot, it replays DB rows and leaves last_seen alone."""
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        _devices_rows(
            {"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}
        ),
    )
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {
                "id": 41,
                "id_urzadzenia": 17,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
                "stan_json": '{"state": "22.5", "on": 1779560000000}',
            }
        ),
    )
    assert store.objects[41].value == "22.5"
    assert store.modules[17].last_seen is None


def test_info_parses_only_safe_fields() -> None:
    """Server info parsing keeps version/ip/mac but drops geo/cloud/private fields."""
    store = _store()
    store.apply(
        f"ampio/fromDB/{USER}/data/info",
        _info(
            serverVersion="1865",
            serverRevision="409",
            mqttVersion="5.133.11",
            local_ip="10.0.0.1",
            device_id="0011223344556677",
            mac="47846",
            # Private fields that must not be stored on AmpioServerInfo.
            lat="51.0",
            lon="17.0",
            city="Some Street",
            cloudInfo="abc.example.com",
            publicKey="xxx",
            perm="0",
        ),
    )
    info = store.server_info
    assert info is not None
    assert info.mac == 47846
    assert info.server_version == "1865"
    assert info.server_revision == "409"
    assert info.mqtt_version == "5.133.11"
    assert info.local_ip == "10.0.0.1"
    assert info.device_id == "0011223344556677"
    stored = {f.name for f in fields(info)}
    for forbidden in ("lat", "lon", "city", "cloudInfo", "publicKey", "perm"):
        assert forbidden not in stored


def test_devices_redelivery_preserves_last_seen() -> None:
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        _devices_rows(
            {"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}
        ),
    )
    store.state.modules[17].last_seen = 1700000000.0
    # Re-deliver the devices list (e.g. on reconnect) - last_seen must persist.
    store.apply(
        DEVICES_TOPIC,
        _devices_rows(
            {"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m2"}
        ),
    )
    assert store.modules[17].name == "m2"
    assert store.modules[17].last_seen == 1700000000.0


def test_state_without_metadata_creates_generic_sensor() -> None:
    store = _store()
    store.apply(
        f"ampio/fromDB/{USER}/ob/93/state",
        '{"state":"187.6","desc":"187.6 "}',
    )
    obj = store.objects[93]
    assert obj.is_sensor is True  # generic fallback
    assert obj.value == "187.6"


@pytest.mark.parametrize(
    "topic_suffix",
    [
        "config/devicesDetails",
        "config/devices",
        "data/states",
        "data/devices",
        "data/params_devices",
    ],
)
def test_handlers_log_and_skip_unparseable_payloads(
    caplog: pytest.LogCaptureFixture, topic_suffix: str
) -> None:
    store = _store()
    with caplog.at_level("WARNING", logger="ampio_mqtt._store"):
        store.apply(f"ampio/fromDB/{USER}/{topic_suffix}", "not json")
    assert "Could not parse" in caplog.text


def test_dispatch_ignores_unmatched_topics() -> None:
    """A topic that matches none of the four patterns is silently ignored."""
    store = _store()
    store.apply("totally/unrelated/topic", "anything")
    assert store.objects == {}
    assert store.modules == {}


def test_state_with_unparseable_payload_is_dropped() -> None:
    """An `/ob/<non-int>/state` topic is rejected without raising."""
    store = _store()
    store.apply(f"ampio/fromDB/{USER}/ob/not-an-int/state", "x")
    assert store.objects == {}


def test_stan_json_with_no_state_field_does_not_overwrite_value() -> None:
    """A stan_json blob without `state` should not clobber an existing value."""
    store = _store()
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {
                "id": 41,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
                "stan_json": '{"on": 1779560000000}',  # no "state"
            }
        ),
    )
    assert store.objects[41].value is None


def test_numeric_value_none_for_bare_nan_state_push() -> None:
    """A bare NaN literal parses (Python's json accepts it) but reads as None."""
    store = _store()
    store.apply(f"ampio/fromDB/{USER}/ob/12/state", '{"state": NaN}')
    obj = store.objects[12]
    assert obj.value == "nan"
    assert obj.numeric_value is None


# --- raw-channel input bridge ---------------------------------------------


def _panel_store() -> AmpioStore:
    """Store that knows panel module 7 (mac CAFE) and a flaga at funkcja 32."""
    store = _store()
    store.apply(DEVICES_TOPIC, _devices_rows(_PANEL))
    store.apply(DETAILS_TOPIC, _details_rows(_flaga_row(50, 32)))
    return store


def test_details_classify_input_and_funkcja() -> None:
    store = _panel_store()
    obj = store.objects[50]
    assert obj.is_input is True
    assert obj.kind is not None and obj.kind.key == "flaga"
    assert obj.funkcja == 32
    assert obj.is_sensor is False


def test_raw_channel_routes_to_input_object_and_notifies() -> None:
    store = _panel_store()
    applied = store.apply("ampio/from/CAFE/state/f/32", "1")

    obj = store.objects[50]
    assert obj.value == "1" and obj.is_on is True
    assert applied.objects == [obj]


def test_raw_channel_unmapped_is_ignored() -> None:
    store = _panel_store()

    # funkcja 5 has no object; a different module mac has no objects at all.
    unmapped = store.apply("ampio/from/CAFE/state/f/5", "1")
    other_mac = store.apply("ampio/from/BEEF/state/f/32", "1")

    assert store.objects[50].value is None
    assert unmapped.objects == [] and other_mac.objects == []


def test_raw_channel_malformed_topic_is_ignored() -> None:
    """A topic that passes the dispatch filter but fails the parser is dropped."""
    store = _panel_store()
    store.apply("ampio/from/CAFE/state/f", "1")  # too short
    assert store.objects[50].value is None


def test_index_rebuilds_when_devices_arrive_after_details() -> None:
    store = _store()
    # Details first: module mac unknown, so the flag is not yet routable.
    store.apply(DETAILS_TOPIC, _details_rows(_flaga_row(50, 32)))
    store.apply("ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].value is None  # not routed - no module mac yet

    # Devices arrive -> index rebuilds -> now routable.
    store.apply(DEVICES_TOPIC, _devices_rows(_PANEL))
    store.apply("ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].value == "1"


def test_flag_without_funkcja_is_not_indexed() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices_rows(_PANEL))
    no_funkcja = {
        "id": 51,
        "id_urzadzenia": 7,
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "opis_menu": "Flag",
    }
    store.apply(DETAILS_TOPIC, _details_rows(no_funkcja))
    assert store._input_index == {}


def test_per_object_echo_dropped_after_raw_seen() -> None:
    """Once raw is seen, the slower per-object echo is suppressed."""
    store = _panel_store()

    raw = store.apply("ampio/from/CAFE/state/f/32", "1")
    assert raw.objects == [store.objects[50]]

    # The lagging per-object republish (note the different "255" encoding).
    echo = store.apply(
        f"ampio/fromDB/{USER}/ob/50/state", '{"state": "255", "on": 1700}'
    )
    assert echo.objects == []  # no double notify
    assert store.objects[50].value == "1"  # fast raw value preserved


def test_mapped_input_without_raw_uses_per_object_fallback() -> None:
    """A mapped input that never produced a raw edge still updates per-object."""
    store = _panel_store()

    applied = store.apply(
        f"ampio/fromDB/{USER}/ob/50/state", '{"state": "255", "on": 1700}'
    )
    obj = store.objects[50]
    assert obj.value == "255" and obj.is_on is True
    assert applied.objects == [obj]


def test_detekcja_routes_via_digital_input_prefix() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices_rows(_PANEL))
    det = {
        "id": 60,
        "id_urzadzenia": 7,
        "typ_komponentu": "detekcja",
        "interpretacja": 1,
        "funkcja": 4,
        "opis_menu": "Motion",
    }
    store.apply(DETAILS_TOPIC, _details_rows(det))
    store.apply("ampio/from/CAFE/state/i/4", "1")
    obj = store.objects[60]
    assert obj.kind is not None and obj.kind.device_class == "motion"
    assert obj.value == "1"


def test_symulacja_classifies_but_is_not_bridged() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices_rows(_PANEL))
    sym = {
        "id": 61,
        "id_urzadzenia": 7,
        "typ_komponentu": "symulacja",
        "interpretacja": 1,
        "funkcja": 1,
        "opis_menu": "Sim",
    }
    store.apply(DETAILS_TOPIC, _details_rows(sym))
    assert store.objects[61].is_input is True
    assert store._input_index == {}  # symulacja prefix not bridged


# --- app-sync data-surface fallback (non-admin accounts) --------------------

DATA_DEVICES_TOPIC = f"ampio/fromDB/{USER}/data/devices"
PARAMS_DEVICES_TOPIC = f"ampio/fromDB/{USER}/data/params_devices"


def _app_row(oid: int, leaf: str, name: str = "Air quality", interp: int = 5) -> dict:
    """One `data/devices` row: the devicesDetails shape minus params/stan_json."""
    return {
        "id": oid,
        "id_urzadzenia": 20,
        "typ_komponentu": "lin_wej",
        "interpretacja": interp,
        "funkcja": 5,
        "leafId": leaf,
        "opis_menu": name,
    }


def test_data_devices_populate_and_classify() -> None:
    store = _store()
    store.apply(
        DATA_DEVICES_TOPIC, _devices_rows(_app_row(24, "0_cb9b_74_0_1", interp=7))
    )
    obj = store.objects[24]
    assert obj.name == "Air quality"
    assert obj.kind is not None and obj.kind.device_class == "carbon_dioxide"
    assert obj.device_id == 20 and obj.funkcja == 5
    assert obj.leaf_id == "0_cb9b_74_0_1"


def test_params_table_before_catalogue_supplies_hidden_flag() -> None:
    """A params table that arrives first is applied when the catalogue lands."""
    store = _store()
    store.apply(
        PARAMS_DEVICES_TOPIC,
        _devices_rows({"id": 24, "params": 17}, {"id": 25, "params": 1}),
    )
    # The table is not grant-filtered; unknown ids create no placeholders.
    assert store.objects == {}

    store.apply(
        DATA_DEVICES_TOPIC,
        _devices_rows(_app_row(24, "0_cb9b_74_0_1"), _app_row(25, "0_cb9b_74_0_2")),
    )
    assert store.objects[24].hidden is True and store.objects[24].visible is False
    assert store.objects[25].hidden is False and store.objects[25].visible is True


def test_params_table_after_catalogue_updates_objects_and_notifies() -> None:
    store = _store()
    store.apply(DATA_DEVICES_TOPIC, _devices_rows(_app_row(24, "0_cb9b_74_0_1")))

    applied = store.apply(
        PARAMS_DEVICES_TOPIC,
        _devices_rows({"id": 24, "params": 17}, {"id": 999, "params": 1}),
    )
    assert store.objects[24].hidden is True
    assert applied.objects == [store.objects[24]]
    assert 999 not in store.objects


def test_data_devices_does_not_degrade_details() -> None:
    """On the admin tier both catalogues arrive; the poorer one must not clobber."""
    store = _store()
    row = _app_row(24, "0_cb9b_74_0_1", name="Named")
    store.apply(
        DETAILS_TOPIC,
        _details_rows({**row, "params": (1 << 37) | 1}),
    )
    store.apply(DATA_DEVICES_TOPIC, _devices_rows(row))
    obj = store.objects[24]
    assert obj.params == (1 << 37) | 1
    assert obj.name == "Named"


# --- cover tilt state ------------------------------------------------------


def test_lammel_is_parsed_into_tilt_position() -> None:
    store = _store()
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {"id": 66, "typ_komponentu": "roleta_lamelki", "interpretacja": 1}
        ),
    )
    store.apply(
        f"ampio/fromDB/{USER}/ob/66/state",
        '{ "state": "95","lammel": "65","block": "0" , "on": 1786723383804}',
    )
    obj = store.objects[66]
    assert obj.value == "95"
    assert obj.tilt_position == 65
    assert obj.supports_tilt is True
    assert obj.is_output is True


def test_plain_cover_reports_no_tilt() -> None:
    store = _store()
    store.apply(
        DETAILS_TOPIC,
        _details_rows(
            {"id": 48, "typ_komponentu": "roleta_procenty", "interpretacja": 1}
        ),
    )
    store.apply(f"ampio/fromDB/{USER}/ob/48/state", '{ "state": "55","block": "0" }')
    obj = store.objects[48]
    assert obj.value == "55"
    assert obj.tilt_position is None
    assert obj.supports_tilt is False


def test_states_snapshot_seeds_tilt_position() -> None:
    store = _store()
    store.apply(
        STATES_TOPIC,
        _states_rows(
            {
                "id": 66,
                "stan_json": '{"state": "100", "lammel": "100", "on": 1779560000000}',
            }
        ),
    )
    assert store.objects[66].tilt_position == 100


# --- module diagnostics ----------------------------------------------------


def _diag_store() -> AmpioStore:
    """Store that knows module 7 at mac 0xCAFE."""
    store = _store()
    store.apply(DEVICES_TOPIC, _devices_rows(_PANEL))
    return store


def test_diagnostics_sets_voltage_and_temperature() -> None:
    store = _diag_store()

    applied = store.apply("ampio/from/CAFE/b/4F", '{"d":[254,79,63,142],"m":51966}')

    module = store.modules[7]
    assert module.supply_voltage == 12.6
    assert module.temperature == 42.0
    assert module.last_seen is not None
    assert applied.modules == [module]


def test_diagnostics_without_a_temperature_sensor_reports_none() -> None:
    """`0` in the temperature byte marks the sensor as absent, not -100 C."""
    store = _diag_store()
    store.apply("ampio/from/CAFE/b/4F", '{"d":[254,79,60,0],"m":51966}')
    module = store.modules[7]
    assert module.supply_voltage == 12.0
    assert module.temperature is None


def test_diagnostics_for_an_unknown_module_is_ignored() -> None:
    store = _diag_store()
    store.apply("ampio/from/BEEF/b/4F", '{"d":[254,79,60,0],"m":48879}')
    assert store.modules[7].supply_voltage is None


@pytest.mark.parametrize(
    "payload",
    [
        '{"d":[254,80,60,0]}',  # not the diagnostics frame type
        '{"d":[1,79,60,0]}',  # not a broadcast
        '{"d":[254,79]}',  # truncated
        "not json",
    ],
)
def test_non_diagnostics_frames_are_ignored(payload: str) -> None:
    store = _diag_store()
    store.apply("ampio/from/CAFE/b/4F", payload)
    assert store.modules[7].supply_voltage is None
