"""The store applies messages without a client, a broker, or an event loop.

These drive `AmpioStore` directly, which is the point of it being separate:
protocol behaviour is reachable from a plain function call, and what a message
changed is a return value rather than something to reconstruct from callbacks.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import fields, replace

import pytest
from conftest import (
    ADMIN_USER,
    DATA_DEVICES_TOPIC,
    DETAILS_TOPIC,
    DEVICES_TOPIC,
    PARAMS_DEVICES_TOPIC,
    STATES_TOPIC,
    USER,
    details,
    devices,
    info,
)

from ampio_mqtt._store import AmpioStore, Applied
from ampio_mqtt.events import (
    BusEvent,
    ModuleRemoved,
    ModuleUpdated,
    ObjectRemoved,
    ObjectUpdated,
)
from ampio_mqtt.models import AmpioModule, AmpioObject


def _updated(applied: Applied) -> list[AmpioObject]:
    return [e.object for e in applied.events if isinstance(e, ObjectUpdated)]


def _removed(applied: Applied) -> list[AmpioObject]:
    return [e.object for e in applied.events if isinstance(e, ObjectRemoved)]


def _mod_updated(applied: Applied) -> list[AmpioModule]:
    return [e.module for e in applied.events if isinstance(e, ModuleUpdated)]


def _mod_removed(applied: Applied) -> list[AmpioModule]:
    return [e.module for e in applied.events if isinstance(e, ModuleRemoved)]


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
        "leafId": "0_a_1_0_0",
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


def test_a_catalogue_reply_reports_its_endpoint_and_the_rows_it_changed() -> None:
    applied = _store().apply(f"ampio/fromDB/{USER}/config/devicesDetails", _catalogue())
    assert applied.endpoint is not None and applied.endpoint.name == "details"
    assert applied.parsed is True
    assert [o.id for o in _updated(applied)] == [41]


def test_an_unchanged_row_reports_nothing() -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    store.apply(topic, _catalogue())
    assert store.apply(topic, _catalogue()).events == []


def test_a_changed_row_reports_only_that_row() -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    store.apply(topic, _catalogue())
    applied = store.apply(topic, _catalogue(opis_menu="Renamed"))
    assert [o.name for o in _updated(applied)] == ["Renamed"]


def test_an_unreadable_reply_reports_its_endpoint_but_not_parsed() -> None:
    """The caller uses this to keep discovery from latching on a bad payload."""
    applied = _store().apply(f"ampio/fromDB/{USER}/config/devicesDetails", "null")
    assert applied.endpoint is not None
    assert applied.parsed is False
    assert _updated(applied) == []


@pytest.mark.parametrize(
    ("topic", "payload", "event_type"),
    [
        (f"ampio/fromDB/{USER}/ob/41/state", '{"state":"1"}', ObjectUpdated),
        ("ampio/from/1/event", "189", BusEvent),
    ],
)
def test_live_messages_carry_no_endpoint(
    topic: str, payload: str, event_type: type
) -> None:
    applied = _store().apply(topic, payload)
    assert applied.endpoint is None
    assert [e for e in applied.events if isinstance(e, event_type)]


def test_an_unrelated_topic_changes_nothing() -> None:
    """A topic matching no routed message kind is silently ignored."""
    store = _store()
    applied = store.apply("totally/unrelated", "anything")
    assert (applied.endpoint, applied.events) == (None, [])
    assert store.objects == {} and store.modules == {}


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

    assert [o.id for o in _updated(applied)] == [50]
    assert store.objects[50].value == "55"


def test_the_store_is_the_only_thing_holding_state() -> None:
    store = _store()
    store.apply(f"ampio/fromDB/{USER}/data/info", '{"Results": {"mac": "47846"}}')
    assert store.server_info is not None and store.server_info.mac == 47846
    assert isinstance(store.objects, dict)
    assert all(isinstance(o, AmpioObject) for o in store.objects.values())


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


def test_a_corrupt_info_reply_never_wipes_held_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The discovery latch never clears, so a True wait_for_initial_discovery
    must keep implying a populated identity on every later read - an
    unparseable info reply must not take it away, and must not trip the
    below-baseline warning off the wiped version."""
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    store.apply(topic, '{"Results": {"mac": 1, "serverVersion": "1865"}}')
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        applied = store.apply(topic, "not json at all")
    assert applied.parsed is False
    assert store.server_info is not None
    assert store.server_info.mac == 1
    assert store.server_info.server_version == "1865"
    assert not any("baseline" in r.getMessage() for r in caplog.records)
    assert any("Could not parse" in r.getMessage() for r in caplog.records)


def test_an_identityless_info_reply_never_wipes_held_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reply that carries no identity cannot take a held one away; the
    refusal is warned so a misbehaving server stays visible."""
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    store.apply(topic, '{"Results": {"mac": 1, "serverVersion": "1865"}}')
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        applied = store.apply(topic, '{"Results": {}}')
    assert applied.parsed is False
    assert store.server_info is not None
    assert store.server_info.mac == 1
    assert any("no server mac" in r.getMessage() for r in caplog.records)


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


def test_snapshots_never_touch_a_raw_proven_object() -> None:
    """A raw-proven object's resync is the broker's retained raw table; a
    DB snapshot may be staler than that raw truth with no comparable clock
    to prove it, so its rows are skipped whatever date they carry."""
    store = _store()
    _raw_proven_flag(store)
    far_future = int((time.time() + 7200) * 1000)
    applied = store.apply(STATES_TOPIC, _snapshot("0", far_future))
    assert _updated(applied) == []
    assert store.objects[10].value == "1"


def test_the_echo_of_a_raw_edge_is_ignored_whole() -> None:
    """The per-object echo repeats what the raw edge delivered ~150 ms
    earlier: no second event, no overwrite, not even its timestamp."""
    store = _store()
    _raw_proven_flag(store)
    before = store.objects[10].updated_at
    applied = store.apply(
        f"ampio/fromDB/{USER}/ob/10/state",
        json.dumps({"state": "255", "on": 1787000000000}),
    )
    assert _updated(applied) == []
    assert store.objects[10].value == "1"
    assert store.objects[10].updated_at == before


def test_a_dated_snapshot_beats_an_undated_seed() -> None:
    store = _store()
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    store.apply(STATES_TOPIC, _snapshot("5", None))
    assert store.objects[10].value == "5"
    assert store.objects[10].updated_at is None
    applied = store.apply(STATES_TOPIC, _snapshot("7", 1779560000000))
    assert [o.id for o in _updated(applied)] == [10]
    assert store.objects[10].value == "7"


def test_an_undated_snapshot_only_fills_a_gap() -> None:
    """An undated report never replaces a value, however the value is dated."""
    store = _store()
    store.apply(f"ampio/fromDB/{USER}/ob/10/state", '{"state":"live"}')
    applied = store.apply(STATES_TOPIC, _snapshot("undated", None))
    assert _updated(applied) == []
    assert store.objects[10].value == "live"


def test_a_newer_snapshot_corrects_a_value_that_changed_during_an_outage() -> None:
    """The snapshot is the only resync after a reconnect (the per-object
    topics are not retained), so a dated-newer report must overwrite the
    value a pre-outage live push left behind."""
    store = _store()
    store.apply(
        f"ampio/fromDB/{USER}/ob/10/state", '{"state":"255","on":1786700100000}'
    )
    assert store.objects[10].value == "255"

    # Reconnect: the object was switched off while the connection was down.
    applied = store.apply(STATES_TOPIC, _snapshot("0", 1786700900000))
    assert [o.id for o in _updated(applied)] == [10]
    assert store.objects[10].value == "0"


def test_echo_of_an_earlier_edge_does_not_disturb_a_fast_toggle() -> None:
    """Edge 1, edge 2, then the echo of edge 1: the value must stay edge 2's
    and nothing may notify - the echo contributes nothing at all."""
    store = _store()
    _raw_proven_flag(store)
    store.apply(f"ampio/from/{0xCAFE:X}/state/f/3", "0")  # edge 2
    before = store.objects[10].updated_at
    applied = store.apply(
        f"ampio/fromDB/{USER}/ob/10/state",
        json.dumps({"state": "255", "on": int(time.time() * 1000)}),  # echo of edge 1
    )
    assert _updated(applied) == []
    assert store.objects[10].value == "0"
    assert store.objects[10].updated_at == before


def test_the_config_catalogue_evicts_what_it_stopped_listing() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1), (11, 2)))
    assert set(store.objects) == {10, 11}

    applied = store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    assert [o.id for o in _removed(applied)] == [11]
    assert set(store.objects) == {10}

    # The unchanged catalogue on the next refresh removes nothing further.
    again = store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    assert _removed(again) == []


def test_the_devices_reply_evicts_missing_modules() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    applied = store.apply(DEVICES_TOPIC, _devices(0xCAFE))
    assert [m.id for m in _mod_removed(applied)] == [2]
    assert set(store.modules) == {1}


def test_an_evicted_objects_raw_channel_no_longer_routes() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1), (11, 2)))
    store.apply(f"ampio/from/{0xBEEF:X}/state/f/3", "1")
    assert store.objects[11].value == "1"

    store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    applied = store.apply(f"ampio/from/{0xBEEF:X}/state/f/3", "0")
    assert _updated(applied) == []
    assert 11 not in store.objects


def test_the_app_sync_catalogue_evicts_only_on_the_restricted_tier() -> None:
    # Admin store: data/devices is a second view, not the authority.
    store = AmpioStore(ADMIN_USER)
    data_topic = f"ampio/fromDB/{ADMIN_USER}/data/devices"
    store.apply(data_topic, _flaga_details((10, 1), (11, 1)))
    applied = store.apply(data_topic, _flaga_details((10, 1)))
    assert _removed(applied) == []
    assert set(store.objects) == {10, 11}

    # Restricted store: the grant bounds it, so the reply is complete for
    # the account and a vanished row is a revocation.
    store = _store()
    data_topic = f"ampio/fromDB/{USER}/data/devices"
    store.apply(data_topic, _flaga_details((10, 1), (11, 1)))
    applied = store.apply(data_topic, _flaga_details((10, 1)))
    assert [o.id for o in _removed(applied)] == [11]
    assert set(store.objects) == {10}


def test_an_empty_catalogue_reply_never_mass_evicts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, _devices(0xCAFE))
    store.apply(DETAILS_TOPIC, _flaga_details((10, 1)))
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        details_applied = store.apply(DETAILS_TOPIC, details())
        devices_applied = store.apply(DEVICES_TOPIC, devices())
    assert _removed(details_applied) == [] and _mod_removed(devices_applied) == []
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
        details(
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
    assert {i for i, o in store.objects.items() if o.is_sensor} == {41, 107}


def test_devices_populate_modules_with_model_and_versions() -> None:
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        devices(
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
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    store.apply(
        DETAILS_TOPIC,
        details(
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


def test_states_snapshot_seeds_value_without_touching_last_seen() -> None:
    """The bulk states reply seeds the value but is not liveness evidence:
    it replays DB state that may be arbitrarily old, so last_seen stays
    None until a live message arrives."""
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    store.apply(
        DETAILS_TOPIC,
        details(
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
        devices(
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
        details(
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
        devices({"id": 41, "stan_json": '{"state": "stale", "on": 1779560000000}'}),
    )
    assert store.objects[41].value == "fresh"


def test_states_snapshot_creates_nothing_for_unknown_ids() -> None:
    """Only the catalogues decide which objects exist. The snapshot replays
    DB rows, ghost rows included - creating from it would later evict an
    object no consumer was ever told existed."""
    store = _store()
    store.apply(
        DATA_DEVICES_TOPIC,
        details({"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"}),
    )
    applied = store.apply(
        STATES_TOPIC,
        devices(
            {"id": 5, "stan_json": '{"state": "1", "on": 1779560000000}'},
            {"id": 999, "stan_json": '{"state": "1", "on": 1779560000000}'},
        ),
    )
    assert 999 not in store.objects
    assert [o.id for o in _updated(applied)] == [5]

    # The catalogue re-request that would have evicted the phantom now
    # removes nothing.
    applied = store.apply(
        DATA_DEVICES_TOPIC,
        details({"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"}),
    )
    assert _removed(applied) == []


def test_snapshot_before_catalogue_seeds_the_value_at_merge() -> None:
    """The snapshot and catalogue replies arrive in no fixed order, and the
    app-sync catalogue carries no stan_json column - a snapshot that lands
    first must still hand the object its value when the catalogue
    establishes it, in the one update that also carries the metadata."""
    store = _store()
    applied = store.apply(
        STATES_TOPIC,
        devices({"id": 20, "stan_json": '{"state": "7", "on": 1779560000000}'}),
    )
    assert 20 not in store.objects
    assert _updated(applied) == []

    applied = store.apply(
        DATA_DEVICES_TOPIC,
        details({"id": 20, "typ_komponentu": "temp", "opis_menu": "T"}),
    )
    assert [o.id for o in _updated(applied)] == [20]
    assert store.objects[20].value == "7"
    assert store.objects[20].updated_at == 1779560000.0
    assert store.objects[20].name == "T"


def test_eviction_prunes_the_buffered_snapshot_value() -> None:
    """An evicted object's buffered seed must not resurface if a later
    catalogue re-establishes the id."""
    store = _store()
    store.apply(
        DATA_DEVICES_TOPIC,
        details(
            {"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"},
            {"id": 6, "typ_komponentu": "flaga", "opis_menu": "G"},
        ),
    )
    store.apply(
        STATES_TOPIC,
        devices(
            {"id": 5, "stan_json": '{"state": "1", "on": 1779560000000}'},
            {"id": 6, "stan_json": '{"state": "1", "on": 1779560000000}'},
        ),
    )
    applied = store.apply(
        DATA_DEVICES_TOPIC,
        details({"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"}),
    )
    assert [o.id for o in _removed(applied)] == [6]

    store.apply(
        DATA_DEVICES_TOPIC,
        details(
            {"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"},
            {"id": 6, "typ_komponentu": "flaga", "opis_menu": "G"},
        ),
    )
    assert store.objects[6].value is None


def test_details_stan_json_seed_does_not_touch_last_seen() -> None:
    """The catalogue's stan_json seed carries state, not liveness - like the
    bulk snapshot, it replays DB rows and leaves last_seen alone."""
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    store.apply(
        DETAILS_TOPIC,
        details(
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
        info(
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
    parsed = store.server_info
    assert parsed is not None
    assert parsed.mac == 47846
    assert parsed.server_version == "1865"
    assert parsed.server_revision == "409"
    assert parsed.mqtt_version == "5.133.11"
    assert parsed.local_ip == "10.0.0.1"
    assert parsed.device_id == "0011223344556677"
    stored = {f.name for f in fields(parsed)}
    for forbidden in ("lat", "lon", "city", "cloudInfo", "publicKey", "perm"):
        assert forbidden not in stored


def test_devices_redelivery_preserves_last_seen() -> None:
    store = _store()
    store.apply(
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    store.modules[17] = replace(store.modules[17], last_seen=1700000000.0)
    # Re-deliver the devices list (e.g. on reconnect) - last_seen must persist.
    store.apply(
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m2"}),
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
        details(
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
    store.apply(DEVICES_TOPIC, devices(_PANEL))
    store.apply(DETAILS_TOPIC, details(_flaga_row(50, 32)))
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
    assert _updated(applied) == [obj]


def test_raw_channel_unmapped_is_ignored() -> None:
    store = _panel_store()

    # funkcja 5 has no object; a different module mac has no objects at all.
    unmapped = store.apply("ampio/from/CAFE/state/f/5", "1")
    other_mac = store.apply("ampio/from/BEEF/state/f/32", "1")

    assert store.objects[50].value is None
    assert _updated(unmapped) == [] and _updated(other_mac) == []


def test_raw_channel_malformed_topic_is_ignored() -> None:
    """A topic that passes the dispatch filter but fails the parser is dropped."""
    store = _panel_store()
    store.apply("ampio/from/CAFE/state/f", "1")  # too short
    assert store.objects[50].value is None


def test_index_rebuilds_when_devices_arrive_after_details() -> None:
    store = _store()
    # Details first: module mac unknown, so the flag is not yet routable.
    store.apply(DETAILS_TOPIC, details(_flaga_row(50, 32)))
    store.apply("ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].value is None  # not routed - no module mac yet

    # Devices arrive -> index rebuilds -> now routable.
    store.apply(DEVICES_TOPIC, devices(_PANEL))
    store.apply("ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].value == "1"


def test_flag_without_funkcja_is_not_bridged() -> None:
    """No channel index means no raw route - an edge must change nothing."""
    store = _store()
    store.apply(DEVICES_TOPIC, devices(_PANEL))
    no_funkcja = {
        "id": 51,
        "id_urzadzenia": 7,
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "opis_menu": "Flag",
    }
    store.apply(DETAILS_TOPIC, details(no_funkcja))
    applied = store.apply("ampio/from/CAFE/state/f/1", "1")
    assert store.objects[51].value is None and _updated(applied) == []


def test_mapped_input_without_raw_uses_per_object_fallback() -> None:
    """A mapped input that never produced a raw edge still updates per-object."""
    store = _panel_store()

    applied = store.apply(
        f"ampio/fromDB/{USER}/ob/50/state", '{"state": "255", "on": 1700}'
    )
    obj = store.objects[50]
    assert obj.value == "255" and obj.is_on is True
    assert _updated(applied) == [obj]


def test_detekcja_routes_via_digital_input_prefix() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, devices(_PANEL))
    det = {
        "id": 60,
        "id_urzadzenia": 7,
        "typ_komponentu": "detekcja",
        "interpretacja": 1,
        "funkcja": 4,
        "opis_menu": "Motion",
    }
    store.apply(DETAILS_TOPIC, details(det))
    store.apply("ampio/from/CAFE/state/i/4", "1")
    obj = store.objects[60]
    assert obj.kind is not None and obj.kind.device_class == "motion"
    assert obj.value == "1"


def test_symulacja_classifies_but_is_not_bridged() -> None:
    store = _store()
    store.apply(DEVICES_TOPIC, devices(_PANEL))
    sym = {
        "id": 61,
        "id_urzadzenia": 7,
        "typ_komponentu": "symulacja",
        "interpretacja": 1,
        "funkcja": 1,
        "opis_menu": "Sim",
    }
    store.apply(DETAILS_TOPIC, details(sym))
    assert store.objects[61].is_input is True
    applied = store.apply("ampio/from/CAFE/state/f/1", "1")
    assert store.objects[61].value is None and _updated(applied) == []


# --- app-sync data-surface fallback (non-admin accounts) --------------------


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
    store.apply(DATA_DEVICES_TOPIC, devices(_app_row(24, "0_cb9b_74_0_1", interp=7)))
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
        devices({"id": 24, "params": 17}, {"id": 25, "params": 1}),
    )
    # The table is not grant-filtered; unknown ids create no placeholders.
    assert store.objects == {}

    store.apply(
        DATA_DEVICES_TOPIC,
        devices(_app_row(24, "0_cb9b_74_0_1"), _app_row(25, "0_cb9b_74_0_2")),
    )
    assert store.objects[24].hidden is True and store.objects[24].visible is False
    assert store.objects[25].hidden is False and store.objects[25].visible is True


def test_params_table_after_catalogue_updates_objects_and_notifies() -> None:
    store = _store()
    store.apply(DATA_DEVICES_TOPIC, devices(_app_row(24, "0_cb9b_74_0_1")))

    applied = store.apply(
        PARAMS_DEVICES_TOPIC,
        devices({"id": 24, "params": 17}, {"id": 999, "params": 1}),
    )
    assert store.objects[24].hidden is True
    assert _updated(applied) == [store.objects[24]]
    assert 999 not in store.objects


def test_data_devices_does_not_degrade_details() -> None:
    """On the admin tier both catalogues arrive; the poorer one must not clobber."""
    store = _store()
    row = _app_row(24, "0_cb9b_74_0_1", name="Named")
    store.apply(
        DETAILS_TOPIC,
        details({**row, "params": (1 << 37) | 1}),
    )
    store.apply(DATA_DEVICES_TOPIC, devices(row))
    obj = store.objects[24]
    assert obj.params == (1 << 37) | 1
    assert obj.name == "Named"


# --- cover tilt state ------------------------------------------------------


def test_lammel_is_parsed_into_tilt_position() -> None:
    store = _store()
    store.apply(
        DETAILS_TOPIC,
        details({"id": 66, "typ_komponentu": "roleta_lamelki", "interpretacja": 1}),
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
        details({"id": 48, "typ_komponentu": "roleta_procenty", "interpretacja": 1}),
    )
    store.apply(f"ampio/fromDB/{USER}/ob/48/state", '{ "state": "55","block": "0" }')
    obj = store.objects[48]
    assert obj.value == "55"
    assert obj.tilt_position is None
    assert obj.supports_tilt is False


def test_states_snapshot_seeds_tilt_position() -> None:
    store = _store()
    store.apply(
        DETAILS_TOPIC,
        details({"id": 66, "typ_komponentu": "roleta_lamelki", "opis_menu": "B"}),
    )
    store.apply(
        STATES_TOPIC,
        devices(
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
    store.apply(DEVICES_TOPIC, devices(_PANEL))
    return store


def test_diagnostics_sets_voltage_and_temperature() -> None:
    store = _diag_store()

    applied = store.apply("ampio/from/CAFE/b/4F", '{"d":[254,79,63,142],"m":51966}')

    module = store.modules[7]
    assert module.supply_voltage == 12.6
    assert module.temperature == 42.0
    assert module.last_seen is not None
    assert _mod_updated(applied) == [module]


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


# --- module catalogue events and event snapshots ---------------------------


def test_devices_reply_dispatches_module_updated_for_new_and_changed() -> None:
    """The module list is news exactly as the object catalogue is: a module
    it adds or changes dispatches ModuleUpdated; an identical re-request
    dispatches nothing."""
    store = _store()
    first = store.apply(DEVICES_TOPIC, _devices(10, 20))
    assert [m.id for m in _mod_updated(first)] == [1, 2]

    again = store.apply(DEVICES_TOPIC, _devices(10, 20))
    assert _mod_updated(again) == []

    doc = json.loads(_devices(10, 20))
    doc["List"][1]["nazwa_urzadzenia"] = "renamed"
    changed = store.apply(DEVICES_TOPIC, json.dumps(doc))
    assert [(m.id, m.name) for m in _mod_updated(changed)] == [(2, "renamed")]


def test_object_updated_carries_a_snapshot() -> None:
    """A dispatched event freezes the state it announced; later changes to
    the same object must not reach a listener that deferred processing.
    Objects are frozen, so the store publishes a new instance per change."""
    store = _store()
    state_topic = f"ampio/fromDB/{USER}/ob/5/state"
    (event,) = _updated(store.apply(state_topic, '{"state": "1", "on": 2000}'))
    store.apply(state_topic, '{"state": "2", "on": 3000}')
    assert event.value == "1"
    assert store.objects[5].value == "2"


def test_module_updated_carries_a_snapshot() -> None:
    store = _diag_store()
    diag_topic = "ampio/from/CAFE/b/4F"
    (event,) = _mod_updated(store.apply(diag_topic, '{"d":[254,79,60,110],"m":0}'))
    store.apply(diag_topic, '{"d":[254,79,70,110],"m":0}')
    assert event.supply_voltage == 12.0
    assert store.modules[7].supply_voltage == 14.0


def test_a_cleared_name_clears_in_the_store() -> None:
    """Every metadata field mirrors the catalogue, name included: an empty
    opis_menu is a normal wire state (unnamed objects), and both discovery
    surfaces agree on names, so there is nothing for a keep-the-old-name
    guard to protect - it only made a server-side clear stick forever."""
    store = _store()
    store.apply(DETAILS_TOPIC, _catalogue(id=9, opis_menu="Old name"))
    assert store.objects[9].name == "Old name"
    applied = store.apply(DETAILS_TOPIC, _catalogue(id=9, opis_menu=""))
    assert store.objects[9].name is None
    assert [o.id for o in _updated(applied)] == [9]


def test_updated_at_takes_the_report_date_or_the_receipt_time() -> None:
    """A dated report stamps the M-SERV's own `on` (0 is a value, not
    absence); an undated push stamps receipt; an undated seed leaves None."""
    store = _store()
    topic = f"ampio/fromDB/{USER}/ob/9/state"
    store.apply(topic, '{"state": "1", "on": 0}')
    assert store.objects[9].updated_at == 0.0
    before = time.time()
    store.apply(topic, '{"state": "2"}')
    updated_at = store.objects[9].updated_at
    assert updated_at is not None and before <= updated_at <= time.time()

    seeded = _store()
    seeded.apply(DETAILS_TOPIC, _flaga_details((9, 1)))
    seeded.apply(
        STATES_TOPIC,
        json.dumps({"List": [{"id": 9, "stan_json": json.dumps({"state": "5"})}]}),
    )
    obj = seeded.objects[9]
    assert obj.value == "5"
    assert obj.updated_at is None


def test_raw_proven_tracks_the_bridge_coverage() -> None:
    """Set by the first raw edge; cleared when the rebuilt index stops
    covering the object, so it goes back to per-object updates."""
    store = _panel_store()
    assert store.objects[50].raw_proven is False
    store.apply("ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].raw_proven is True
    retyped = dict(_flaga_row(50, 32), typ_komponentu="roleta_procenty")
    store.apply(DETAILS_TOPIC, details(retyped))
    assert store.objects[50].raw_proven is False
