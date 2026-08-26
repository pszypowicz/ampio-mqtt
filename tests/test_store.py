"""The store applies messages without a client, a broker, or an event loop.

These drive `AmpioStore` directly, which is the point of it being separate:
protocol behaviour is reachable from a plain function call, and what a message
changed is a return value rather than something to reconstruct from callbacks.
Tests speak in wire topics for readability; :func:`_apply` routes them the
way the client dispatcher does, with the tier-scoping the client applies
left off - the store is tier-agnostic and each test feeds one tier's wire.
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

from ampio_mqtt import _protocol
from ampio_mqtt._protocol import ENDPOINTS, DesignerResolution, Router
from ampio_mqtt._store import AmpioStore, Applied
from ampio_mqtt.classification import (
    InputKind,
    OutputKind,
    SensorKind,
    ThermostatKind,
)
from ampio_mqtt.events import (
    BusEvent,
    ModuleRemoved,
    ModuleUpdated,
    ObjectAdded,
    ObjectRemoved,
    ObjectUpdated,
)
from ampio_mqtt.models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    ThermostatState,
)


def _updated(applied: Applied) -> list[AmpioObject]:
    return [e.object for e in applied.events if isinstance(e, ObjectUpdated)]


def _removed(applied: Applied) -> list[AmpioObject]:
    return [e.object for e in applied.events if isinstance(e, ObjectRemoved)]


def _mod_updated(applied: Applied) -> list[AmpioModule]:
    return [e.module for e in applied.events if isinstance(e, ModuleUpdated)]


def _mod_removed(applied: Applied) -> list[AmpioModule]:
    return [e.module for e in applied.events if isinstance(e, ModuleRemoved)]


_ROUTERS = {
    USER: Router(USER, ENDPOINTS),
    ADMIN_USER: Router(ADMIN_USER, ENDPOINTS),
}


def _apply(store: AmpioStore, topic: str, payload: str, *, user: str = USER) -> Applied:
    """Route a wire message and apply it, as the client dispatcher does.

    An unroutable topic applies nothing, exactly as the dispatcher drops it.
    """
    msg = _ROUTERS[user].route(topic, payload)
    return store.apply(msg) if msg is not None else Applied()


def _store() -> AmpioStore:
    return AmpioStore()


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


def test_a_catalogue_reply_reports_parsed_and_the_rows_it_changed() -> None:
    applied = _apply(
        _store(), f"ampio/fromDB/{USER}/config/devicesDetails", _catalogue()
    )
    assert applied.parsed is True
    assert [o.id for o in _updated(applied)] == [41]


def test_an_unchanged_row_reports_nothing() -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    _apply(store, topic, _catalogue())
    assert _apply(store, topic, _catalogue()).events == []


def test_a_changed_row_reports_only_that_row() -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    _apply(store, topic, _catalogue())
    applied = _apply(store, topic, _catalogue(opis_menu="Renamed"))
    assert [o.name for o in _updated(applied)] == ["Renamed"]


def test_an_unreadable_reply_reports_not_parsed() -> None:
    """The caller uses this to keep discovery from latching on a bad payload."""
    applied = _apply(_store(), f"ampio/fromDB/{USER}/config/devicesDetails", "null")
    assert applied.parsed is False
    assert _updated(applied) == []


@pytest.mark.parametrize(
    ("topic", "payload", "event_type"),
    [
        (f"ampio/fromDB/{USER}/ob/41/state", '{"state":"1"}', ObjectUpdated),
        ("ampio/from/1/event", "189", BusEvent),
    ],
)
def test_live_messages_dispatch_their_event(
    topic: str, payload: str, event_type: type
) -> None:
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 41}))
    applied = _apply(store, topic, payload)
    assert [e for e in applied.events if isinstance(e, event_type)]


def test_an_unrelated_topic_changes_nothing() -> None:
    """A topic matching no routed message kind is silently ignored."""
    store = _store()
    applied = _apply(store, "totally/unrelated", "anything")
    assert applied.events == []
    assert store.objects == {} and store.modules == {}


def test_an_object_leaving_the_index_is_freed_from_raw_suppression() -> None:
    """An id recycled onto a type the raw tree does not carry must not freeze.

    DB ids are reassigned when a module is replaced, so a raw-proven flag can
    come back as something else entirely - which no raw channel feeds.
    """
    store = _store()
    _apply(
        store,
        f"ampio/fromDB/{USER}/config/devices",
        json.dumps({"List": [{"id": 7, "mac": 0xCAFE, "typ_urzadzenia": 11}]}),
    )
    _apply(
        store,
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
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].value == "1"

    # After a module swap the id comes back as a cover, which no raw channel
    # feeds, so its only updates are the per-object ones.
    _apply(
        store,
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
    applied = _apply(store, f"ampio/fromDB/{USER}/ob/50/state", '{"state":"55"}')

    assert [o.id for o in _updated(applied)] == [50]
    assert store.objects[50].value == "55"


def test_the_store_is_the_only_thing_holding_state() -> None:
    store = _store()
    _apply(store, f"ampio/fromDB/{USER}/data/info", '{"Results": {"mac": "47846"}}')
    assert store.server_info is not None and store.server_info.mac == 47846
    assert isinstance(store.objects, dict)
    assert all(isinstance(o, AmpioObject) for o in store.objects.values())


def test_a_below_baseline_server_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    payload = '{"Results": {"mac": 1, "serverVersion": "409"}}'
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        _apply(store, topic, payload)
        # The re-request every reconnect issues repeats the same version.
        _apply(store, topic, payload)
    warnings = [r for r in caplog.records if "baseline" in r.getMessage()]
    assert len(warnings) == 1
    assert "409" in warnings[0].getMessage()


def test_a_baseline_server_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    store = _store()
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        _apply(
            store,
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
    _apply(store, topic, '{"Results": {"mac": 1, "serverVersion": "1865"}}')
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        applied = _apply(store, topic, "not json at all")
    assert applied.parsed is False
    assert store.server_info is not None
    assert store.server_info.mac == 1
    assert store.server_info.server_version == "1865"
    assert not any("baseline" in r.getMessage() for r in caplog.records)
    assert any("Could not parse" in r.getMessage() for r in caplog.records)


def test_an_identityless_info_reply_never_wipes_held_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reply without a server mac is unparseable, so it neither takes a
    held identity away nor is stored - `AmpioServerInfo.key` stays
    populated by construction."""
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    _apply(store, topic, '{"Results": {"mac": 1, "serverVersion": "1865"}}')
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        applied = _apply(store, topic, '{"Results": {}}')
    assert applied.parsed is False
    assert store.server_info is not None
    assert store.server_info.mac == 1
    assert any("Could not parse" in r.getMessage() for r in caplog.records)


def test_below_baseline_warning_survives_an_identityless_reply_arriving_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An identity-less reply is never stored, so it cannot pre-seat the
    version and suppress the warning the identified reply should trip."""
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        _apply(store, topic, '{"Results": {"serverVersion": "100"}}')
        _apply(store, topic, '{"Results": {"mac": 1, "serverVersion": "100"}}')
    warnings = [r for r in caplog.records if "baseline" in r.getMessage()]
    assert len(warnings) == 1
    assert "100" in warnings[0].getMessage()


def test_handler_table_misalignment_fails_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler-gated endpoint row without a matching store handler (a
    name typo, a row added without its handler) would surface as a silent
    discovery hang; construction refuses it instead."""
    rogue = _protocol.Endpoint("rogue", "data", "rogue", "data", "rogue")
    monkeypatch.setattr(_protocol, "ENDPOINTS", (*ENDPOINTS, rogue))
    with pytest.raises(RuntimeError, match="rogue"):
        AmpioStore()


def _raw_proven_flag(store: AmpioStore, mac: int = 0xCAFE) -> None:
    """Discover one flaga (ob/10 on module 1, channel f/3) and land a raw edge."""
    _apply(store, DEVICES_TOPIC, _devices(mac))
    _apply(store, DETAILS_TOPIC, _flaga_details((10, 1)))
    _apply(store, f"ampio/from/{mac:X}/state/f/3", "1")


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
    applied = _apply(store, STATES_TOPIC, _snapshot("0", far_future))
    assert _updated(applied) == []
    assert store.objects[10].value == "1"


def test_the_echo_of_a_raw_edge_is_ignored_whole() -> None:
    """The per-object echo repeats what the raw edge delivered ~150 ms
    earlier: no second event, no overwrite, not even its timestamp."""
    store = _store()
    _raw_proven_flag(store)
    before = store.objects[10].updated_at
    applied = _apply(
        store,
        f"ampio/fromDB/{USER}/ob/10/state",
        json.dumps({"state": "255", "on": 1787000000000}),
    )
    assert _updated(applied) == []
    assert store.objects[10].value == "1"
    assert store.objects[10].updated_at == before


def test_a_dated_snapshot_beats_an_undated_seed() -> None:
    store = _store()
    _apply(store, DETAILS_TOPIC, _flaga_details((10, 1)))
    _apply(store, STATES_TOPIC, _snapshot("5", None))
    assert store.objects[10].value == "5"
    assert store.objects[10].updated_at is None
    applied = _apply(store, STATES_TOPIC, _snapshot("7", 1779560000000))
    assert [o.id for o in _updated(applied)] == [10]
    assert store.objects[10].value == "7"


def test_an_undated_snapshot_only_fills_a_gap() -> None:
    """An undated report never replaces a value, however the value is dated."""
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 10}))
    _apply(store, f"ampio/fromDB/{USER}/ob/10/state", '{"state":"live"}')
    applied = _apply(store, STATES_TOPIC, _snapshot("undated", None))
    assert _updated(applied) == []
    assert store.objects[10].value == "live"


def test_a_newer_snapshot_corrects_a_value_that_changed_during_an_outage() -> None:
    """The snapshot is the only resync after a reconnect (the per-object
    topics are not retained), so a dated-newer report must overwrite the
    value a pre-outage live push left behind."""
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 10}))
    _apply(
        store, f"ampio/fromDB/{USER}/ob/10/state", '{"state":"255","on":1786700100000}'
    )
    assert store.objects[10].value == "255"

    # Reconnect: the object was switched off while the connection was down.
    applied = _apply(store, STATES_TOPIC, _snapshot("0", 1786700900000))
    assert [o.id for o in _updated(applied)] == [10]
    assert store.objects[10].value == "0"


def test_a_skewed_dated_snapshot_does_not_displace_a_live_undated_push() -> None:
    """An undated push carries this process's clock, which a server `on`
    stamp cannot outrank however far ahead the M-SERV's RTC runs. The
    live value stands until the next snapshot request cycle."""
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 10}))
    _apply(store, f"ampio/fromDB/{USER}/ob/10/state", '{"state":"live"}')
    far_future = int((time.time() + 3600) * 1000)
    applied = _apply(store, STATES_TOPIC, _snapshot("stale", far_future))
    assert _updated(applied) == []
    assert store.objects[10].value == "live"


def test_begin_refresh_lets_the_snapshot_resync_an_undated_value() -> None:
    """A new request cycle proves the next snapshot is at least as fresh
    as anything held, so the dated seed corrects the pre-cycle push."""
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 10}))
    _apply(store, f"ampio/fromDB/{USER}/ob/10/state", '{"state":"live"}')
    store.begin_refresh()
    applied = _apply(store, STATES_TOPIC, _snapshot("0", 1786700900000))
    assert [o.id for o in _updated(applied)] == [10]
    assert store.objects[10].value == "0"


def test_a_buffered_undated_push_beats_a_skewed_stan_json_seed() -> None:
    """The pending replay makes no cross-clock comparison either: the
    push arrived live in this session, so the row's dated seed loses."""
    store = _store()
    far_future = int((time.time() + 3600) * 1000)
    _apply(store, f"ampio/fromDB/{USER}/ob/93/state", '{"state":"live"}')
    row = {"id": 93, "stan_json": json.dumps({"state": "stale", "on": far_future})}
    _apply(store, DETAILS_TOPIC, details(row))
    assert store.objects[93].value == "live"


def test_echo_of_an_earlier_edge_does_not_disturb_a_fast_toggle() -> None:
    """Edge 1, edge 2, then the echo of edge 1: the value must stay edge 2's
    and nothing may notify - the echo contributes nothing at all."""
    store = _store()
    _raw_proven_flag(store)
    _apply(store, f"ampio/from/{0xCAFE:X}/state/f/3", "0")  # edge 2
    before = store.objects[10].updated_at
    applied = _apply(
        store,
        f"ampio/fromDB/{USER}/ob/10/state",
        json.dumps({"state": "255", "on": int(time.time() * 1000)}),  # echo of edge 1
    )
    assert _updated(applied) == []
    assert store.objects[10].value == "0"
    assert store.objects[10].updated_at == before


def test_the_config_catalogue_evicts_what_it_stopped_listing() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    _apply(store, DETAILS_TOPIC, _flaga_details((10, 1), (11, 2)))
    assert set(store.objects) == {10, 11}

    applied = _apply(store, DETAILS_TOPIC, _flaga_details((10, 1)))
    assert [o.id for o in _removed(applied)] == [11]
    assert set(store.objects) == {10}

    # The unchanged catalogue on the next refresh removes nothing further.
    again = _apply(store, DETAILS_TOPIC, _flaga_details((10, 1)))
    assert _removed(again) == []


def test_the_devices_reply_evicts_missing_modules() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    applied = _apply(store, DEVICES_TOPIC, _devices(0xCAFE))
    assert [m.id for m in _mod_removed(applied)] == [2]
    assert set(store.modules) == {1}


def test_an_evicted_objects_raw_channel_no_longer_routes() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    _apply(store, DETAILS_TOPIC, _flaga_details((10, 1), (11, 2)))
    _apply(store, f"ampio/from/{0xBEEF:X}/state/f/3", "1")
    assert store.objects[11].value == "1"

    _apply(store, DETAILS_TOPIC, _flaga_details((10, 1)))
    applied = _apply(store, f"ampio/from/{0xBEEF:X}/state/f/3", "0")
    assert _updated(applied) == []
    assert 11 not in store.objects


def test_a_tier_scoped_router_leaves_the_other_tiers_surfaces_unroutable() -> None:
    """The store treats every catalogue reply as complete for its account
    because the client routes only the tier's served endpoints: an admin
    router never yields the app-sync catalogue (a differently-scoped view
    that must not evict), and a restricted router never yields config."""
    admin = Router(
        ADMIN_USER,
        tuple(ep for ep in ENDPOINTS if ep.tier in (None, AccessTier.ADMIN)),
    )
    restricted = Router(
        USER,
        tuple(ep for ep in ENDPOINTS if ep.tier in (None, AccessTier.RESTRICTED)),
    )
    assert admin.route(f"ampio/fromDB/{ADMIN_USER}/data/devices", "{}") is None
    assert restricted.route(DETAILS_TOPIC, "{}") is None
    assert restricted.route(DEVICES_TOPIC, "{}") is None


def test_the_app_sync_catalogue_evicts_what_the_grant_revoked() -> None:
    # The grant bounds a restricted store, so the reply is complete for
    # the account and a vanished row is a revocation.
    store = _store()
    data_topic = f"ampio/fromDB/{USER}/data/devices"
    _apply(store, data_topic, _flaga_details((10, 1), (11, 1)))
    applied = _apply(store, data_topic, _flaga_details((10, 1)))
    assert [o.id for o in _removed(applied)] == [11]
    assert set(store.objects) == {10}


def test_an_empty_catalogue_reply_evicts_everything() -> None:
    """An empty reply is a complete reply listing nothing - a full grant
    revocation on the app-sync surface, a wiped configuration on config -
    and evicts like any other, one removal event per object and module."""
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE))
    _apply(store, DETAILS_TOPIC, _flaga_details((10, 1)))
    details_applied = _apply(store, DETAILS_TOPIC, details())
    devices_applied = _apply(store, DEVICES_TOPIC, devices())
    assert [o.id for o in _removed(details_applied)] == [10]
    assert [m.id for m in _mod_removed(devices_applied)] == [1]
    assert store.objects == {} and store.modules == {}


def test_live_messages_touch_last_seen_snapshots_do_not() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE))
    _apply(store, DETAILS_TOPIC, _flaga_details((10, 1)))
    assert store.modules[1].last_seen is None

    _apply(store, STATES_TOPIC, _snapshot("1", 1779560000000))
    assert store.modules[1].last_seen is None

    before = time.time()
    _apply(store, f"ampio/from/{0xCAFE:X}/state/f/3", "1")
    seen = store.modules[1].last_seen
    assert seen is not None and before <= seen <= time.time()


# --- catalogues, state pushes, and snapshots --------------------------------


def test_details_populate_and_classify() -> None:
    store = _store()
    _apply(
        store,
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
                "type": "266",  # 0x010A On/Off Plug-in Unit
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
    assert not isinstance(store.objects[1].kind, SensorKind)
    # The Designer's Matter device type tag rides the merge; untagged rows
    # read None.
    assert store.objects[1].matter_device_type == 266
    assert store.objects[41].matter_device_type is None
    assert {i for i, o in store.objects.items() if isinstance(o.kind, SensorKind)} == {
        41,
        107,
    }


def test_devices_populate_modules_with_model_and_versions() -> None:
    store = _store()
    _apply(
        store,
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
    _apply(
        store,
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    _apply(
        store,
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
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/41/state",
        '{"state": "22.5", "on": 1779565263813}',
    )
    first_seen = store.modules[17].last_seen
    assert first_seen is not None and before <= first_seen <= time.time()

    # Another push refreshes it, regardless of its server date being older.
    _apply(
        store,
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
    _apply(
        store,
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    _apply(
        store,
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

    _apply(
        store,
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
    _apply(
        store,
        DETAILS_TOPIC,
        details(
            {"id": 41, "typ_komponentu": "temp", "interpretacja": 1, "opis_menu": "T"}
        ),
    )
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/41/state",
        '{"state": "fresh", "on": 1779570000000}',
    )
    assert store.objects[41].value == "fresh"

    _apply(
        store,
        STATES_TOPIC,
        devices({"id": 41, "stan_json": '{"state": "stale", "on": 1779560000000}'}),
    )
    assert store.objects[41].value == "fresh"


def test_states_snapshot_creates_nothing_for_unknown_ids() -> None:
    """Only the catalogues decide which objects exist. The snapshot replays
    DB rows, ghost rows included - creating from it would later evict an
    object no consumer was ever told existed."""
    store = _store()
    _apply(
        store,
        DATA_DEVICES_TOPIC,
        details({"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"}),
    )
    applied = _apply(
        store,
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
    applied = _apply(
        store,
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
    applied = _apply(
        store,
        STATES_TOPIC,
        devices({"id": 20, "stan_json": '{"state": "7", "on": 1779560000000}'}),
    )
    assert 20 not in store.objects
    assert _updated(applied) == []

    applied = _apply(
        store,
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
    _apply(
        store,
        DATA_DEVICES_TOPIC,
        details(
            {"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"},
            {"id": 6, "typ_komponentu": "flaga", "opis_menu": "G"},
        ),
    )
    _apply(
        store,
        STATES_TOPIC,
        devices(
            {"id": 5, "stan_json": '{"state": "1", "on": 1779560000000}'},
            {"id": 6, "stan_json": '{"state": "1", "on": 1779560000000}'},
        ),
    )
    applied = _apply(
        store,
        DATA_DEVICES_TOPIC,
        details({"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"}),
    )
    assert [o.id for o in _removed(applied)] == [6]

    _apply(
        store,
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
    _apply(
        store,
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    _apply(
        store,
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
    _apply(
        store,
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
    _apply(
        store,
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    store.modules[17] = replace(store.modules[17], last_seen=1700000000.0)
    # Re-deliver the devices list (e.g. on reconnect) - last_seen must persist.
    _apply(
        store,
        DEVICES_TOPIC,
        devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m2"}),
    )
    assert store.modules[17].name == "m2"
    assert store.modules[17].last_seen == 1700000000.0


def test_a_push_for_an_uncatalogued_id_waits_for_its_catalogue_row() -> None:
    """Only the catalogues decide which objects exist. A push that races
    ahead of them dispatches nothing and creates nothing; the catalogue
    row surfaces the object already carrying the buffered value, so there
    is no update/remove churn around a catalogue reply."""
    store = _store()
    state_topic = f"ampio/fromDB/{USER}/ob/93/state"
    applied = _apply(store, state_topic, '{"state":"187.6","desc":"187.6 "}')
    assert applied.events == []
    assert 93 not in store.objects

    applied = _apply(store, DETAILS_TOPIC, details({"id": 93}))
    obj = store.objects[93]
    assert isinstance(obj.kind, SensorKind)  # no typ_komponentu -> fallback
    assert obj.value == "187.6"
    assert [o.value for o in _updated(applied)] == ["187.6"]


def test_a_buffered_push_loses_to_a_newer_dated_stan_json_seed() -> None:
    """The catalogue replay uses the same dated-supersedes rule as the
    snapshot: the fresher of push and stan_json seed wins."""
    store = _store()
    state_topic = f"ampio/fromDB/{USER}/ob/93/state"
    _apply(store, state_topic, '{"state":"old","on":1000}')
    row = {"id": 93, "stan_json": json.dumps({"state": "new", "on": 2000})}
    _apply(store, DETAILS_TOPIC, details(row))
    assert store.objects[93].value == "new"

    fresh = _store()
    _apply(fresh, state_topic, '{"state":"newer","on":3000}')
    _apply(fresh, DETAILS_TOPIC, details(row))
    assert fresh.objects[93].value == "newer"


def test_a_buffered_push_for_a_ghost_id_is_pruned_by_a_complete_catalogue() -> None:
    """A catalogue that does not list the pushed id proves it will never
    gain a row; the buffered value must not resurface if the id later
    appears (a DB id reassignment, not the same object)."""
    store = _store()
    _apply(store, f"ampio/fromDB/{USER}/ob/99/state", '{"state":"ghost"}')
    _apply(store, DETAILS_TOPIC, details({"id": 41}))
    _apply(store, DETAILS_TOPIC, details({"id": 41}, {"id": 99}))
    assert store.objects[99].value is None


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
        _apply(store, f"ampio/fromDB/{USER}/{topic_suffix}", "not json")
    assert "Could not parse" in caplog.text


def test_state_with_unparseable_payload_is_dropped() -> None:
    """An `/ob/<non-int>/state` topic is rejected without raising."""
    store = _store()
    _apply(store, f"ampio/fromDB/{USER}/ob/not-an-int/state", "x")
    assert store.objects == {}


def test_stan_json_with_no_state_field_does_not_overwrite_value() -> None:
    """A stan_json blob without `state` should not clobber an existing value."""
    store = _store()
    _apply(
        store,
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
    _apply(store, DETAILS_TOPIC, details({"id": 12}))
    _apply(store, f"ampio/fromDB/{USER}/ob/12/state", '{"state": NaN}')
    obj = store.objects[12]
    assert obj.value == "nan"
    assert obj.numeric_value is None


# --- raw-channel input bridge ---------------------------------------------


def _panel_store() -> AmpioStore:
    """Store that knows panel module 7 (mac CAFE) and a flaga at funkcja 32."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _apply(store, DETAILS_TOPIC, details(_flaga_row(50, 32)))
    return store


def test_details_classify_input_and_funkcja() -> None:
    store = _panel_store()
    obj = store.objects[50]
    assert isinstance(obj.kind, InputKind)
    assert obj.kind.key == "flaga"
    assert obj.funkcja == 32
    assert not isinstance(obj.kind, SensorKind)


def test_raw_channel_routes_to_input_object_and_notifies() -> None:
    store = _panel_store()
    applied = _apply(store, "ampio/from/CAFE/state/f/32", "1")

    obj = store.objects[50]
    assert obj.value == "1" and obj.is_on is True
    assert _updated(applied) == [obj]


def test_raw_channel_unmapped_is_ignored() -> None:
    store = _panel_store()

    # funkcja 5 has no object; a different module mac has no objects at all.
    unmapped = _apply(store, "ampio/from/CAFE/state/f/5", "1")
    other_mac = _apply(store, "ampio/from/BEEF/state/f/32", "1")

    assert store.objects[50].value is None
    assert _updated(unmapped) == [] and _updated(other_mac) == []


def test_raw_channel_malformed_topic_is_ignored() -> None:
    """A topic that passes the dispatch filter but fails the parser is dropped."""
    store = _panel_store()
    _apply(store, "ampio/from/CAFE/state/f", "1")  # too short
    assert store.objects[50].value is None


def test_index_rebuilds_when_devices_arrive_after_details() -> None:
    store = _store()
    # Details first: module mac unknown, so the flag is not yet routable.
    _apply(store, DETAILS_TOPIC, details(_flaga_row(50, 32)))
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].value is None  # not routed - no module mac yet

    # Devices arrive -> index rebuilds -> now routable.
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].value == "1"


def test_flag_without_funkcja_is_not_bridged() -> None:
    """No channel index means no raw route - an edge must change nothing."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    no_funkcja = {
        "id": 51,
        "id_urzadzenia": 7,
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "opis_menu": "Flag",
    }
    _apply(store, DETAILS_TOPIC, details(no_funkcja))
    applied = _apply(store, "ampio/from/CAFE/state/f/1", "1")
    assert store.objects[51].value is None and _updated(applied) == []


def test_mapped_input_without_raw_uses_per_object_fallback() -> None:
    """A mapped input that never produced a raw edge still updates per-object."""
    store = _panel_store()

    applied = _apply(
        store, f"ampio/fromDB/{USER}/ob/50/state", '{"state": "255", "on": 1700}'
    )
    obj = store.objects[50]
    assert obj.value == "255" and obj.is_on is True
    assert _updated(applied) == [obj]


def test_detekcja_routes_via_digital_input_prefix() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    det = {
        "id": 60,
        "id_urzadzenia": 7,
        "typ_komponentu": "detekcja",
        "interpretacja": 1,
        "funkcja": 4,
        "opis_menu": "Motion",
    }
    _apply(store, DETAILS_TOPIC, details(det))
    _apply(store, "ampio/from/CAFE/state/i/4", "1")
    obj = store.objects[60]
    assert obj.kind is not None and obj.kind.device_class == "motion"
    assert obj.value == "1"


def test_wej_routes_via_digital_input_prefix() -> None:
    """A physical-input object (#117) bridges on `i/<funkcja>` like detekcja."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    wej = {
        "id": 62,
        "id_urzadzenia": 7,
        "typ_komponentu": "wej",
        "interpretacja": 1,
        "funkcja": 1,
        "opis_menu": "Button",
    }
    _apply(store, DETAILS_TOPIC, details(wej))
    obj = store.objects[62]
    assert isinstance(obj.kind, InputKind)
    assert obj.kind.key == "wej" and obj.kind.device_class is None
    _apply(store, "ampio/from/CAFE/state/i/1", "1")
    assert store.objects[62].value == "1" and store.objects[62].is_on is True


def test_wej_per_object_edge_reads_255_as_on() -> None:
    """The per-object path (both tiers) publishes 255 pressed / 0 released."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    wej = {
        "id": 63,
        "id_urzadzenia": 7,
        "typ_komponentu": "wej",
        "interpretacja": 1,
        "funkcja": 2,
        "opis_menu": "Button",
    }
    _apply(store, DETAILS_TOPIC, details(wej))
    _apply(store, f"ampio/fromDB/{USER}/ob/63/state", '{"state": "255", "on": 1700}')
    assert store.objects[63].is_on is True
    _apply(store, f"ampio/fromDB/{USER}/ob/63/state", '{"state": "0", "on": 1701}')
    assert store.objects[63].is_on is False


def test_symulacja_classifies_but_is_not_bridged() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    sym = {
        "id": 61,
        "id_urzadzenia": 7,
        "typ_komponentu": "symulacja",
        "interpretacja": 1,
        "funkcja": 1,
        "opis_menu": "Sim",
    }
    _apply(store, DETAILS_TOPIC, details(sym))
    assert isinstance(store.objects[61].kind, InputKind)
    applied = _apply(store, "ampio/from/CAFE/state/f/1", "1")
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
    _apply(store, DATA_DEVICES_TOPIC, devices(_app_row(24, "0_cb9b_74_0_1", interp=7)))
    obj = store.objects[24]
    assert obj.name == "Air quality"
    assert obj.kind is not None and obj.kind.device_class == "carbon_dioxide"
    assert obj.device_id == 20 and obj.funkcja == 5
    assert obj.leaf_id == "0_cb9b_74_0_1"


def test_params_table_before_catalogue_supplies_hidden_flag() -> None:
    """A params table that arrives first is applied when the catalogue lands."""
    store = _store()
    _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        devices({"id": 24, "params": 17}, {"id": 25, "params": 1}),
    )
    # The table is not grant-filtered; unknown ids create no placeholders.
    assert store.objects == {}

    _apply(
        store,
        DATA_DEVICES_TOPIC,
        devices(_app_row(24, "0_cb9b_74_0_1"), _app_row(25, "0_cb9b_74_0_2")),
    )
    assert store.objects[24].hidden is True and store.objects[24].visible is False
    assert store.objects[25].hidden is False and store.objects[25].visible is True


def test_params_table_after_catalogue_updates_objects_and_notifies() -> None:
    store = _store()
    _apply(store, DATA_DEVICES_TOPIC, devices(_app_row(24, "0_cb9b_74_0_1")))

    applied = _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        devices({"id": 24, "params": 17}, {"id": 999, "params": 1}),
    )
    assert store.objects[24].hidden is True
    assert _updated(applied) == [store.objects[24]]
    assert 999 not in store.objects


# --- cover tilt state ------------------------------------------------------


def test_lammel_is_parsed_into_tilt_position() -> None:
    store = _store()
    _apply(
        store,
        DETAILS_TOPIC,
        details({"id": 66, "typ_komponentu": "roleta_lamelki", "interpretacja": 1}),
    )
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/66/state",
        '{ "state": "95","lammel": "65","block": "0" , "on": 1786723383804}',
    )
    obj = store.objects[66]
    assert obj.value == "95"
    assert obj.tilt_position == 65
    assert obj.supports_tilt is True
    assert isinstance(obj.kind, OutputKind)


def test_plain_cover_reports_no_tilt() -> None:
    store = _store()
    _apply(
        store,
        DETAILS_TOPIC,
        details({"id": 48, "typ_komponentu": "roleta_procenty", "interpretacja": 1}),
    )
    _apply(store, f"ampio/fromDB/{USER}/ob/48/state", '{ "state": "55","block": "0" }')
    obj = store.objects[48]
    assert obj.value == "55"
    assert obj.tilt_position is None
    assert obj.supports_tilt is False


def test_states_snapshot_seeds_tilt_position() -> None:
    store = _store()
    _apply(
        store,
        DETAILS_TOPIC,
        details({"id": 66, "typ_komponentu": "roleta_lamelki", "opis_menu": "B"}),
    )
    _apply(
        store,
        STATES_TOPIC,
        devices(
            {
                "id": 66,
                "stan_json": '{"state": "100", "lammel": "100", "on": 1779560000000}',
            }
        ),
    )
    assert store.objects[66].tilt_position == 100


# --- reg climate readback --------------------------------------------------

# A reg state as a live M-SERV serializes it: every field a string.
REG_PAYLOAD = (
    '{ "state": "0", "cooling": "0", "mode": "S",'
    '"measureTemp": "25.90","setTemperature": "21.00", "on": 1787682427583}'
)
REG_READBACK = ThermostatState(
    measured_temperature=25.9,
    target_temperature=21.0,
    mode="S",
    cooling=False,
)


def test_reg_push_carries_thermostat_readback() -> None:
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 138, "typ_komponentu": "reg"}))
    _apply(store, f"ampio/fromDB/{USER}/ob/138/state", REG_PAYLOAD)
    obj = store.objects[138]
    assert obj.value == "0"
    assert isinstance(obj.kind, ThermostatKind)
    assert obj.thermostat == REG_READBACK


def test_plain_push_keeps_last_readback() -> None:
    """A later report without the reg shape keeps the readback, like tilt."""
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 138, "typ_komponentu": "reg"}))
    _apply(store, f"ampio/fromDB/{USER}/ob/138/state", REG_PAYLOAD)
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/138/state",
        '{"state": "1", "on": 1787682500000}',
    )
    obj = store.objects[138]
    assert obj.value == "1"
    assert obj.thermostat == REG_READBACK


def test_snapshot_readback_change_alone_dispatches() -> None:
    """A dated snapshot that moves only the readback still reports the
    object changed - a climate consumer must see the temperature tick."""
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 138, "typ_komponentu": "reg"}))
    _apply(store, f"ampio/fromDB/{USER}/ob/138/state", REG_PAYLOAD)
    newer = REG_PAYLOAD.replace('"25.90"', '"26.40"').replace(
        "1787682427583", "1787682600000"
    )
    applied = _apply(store, STATES_TOPIC, devices({"id": 138, "stan_json": newer}))
    assert [o.id for o in _updated(applied)] == [138]
    assert store.objects[138].thermostat is not None
    assert store.objects[138].thermostat.measured_temperature == 26.4


def test_states_snapshot_seeds_thermostat() -> None:
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 138, "typ_komponentu": "reg"}))
    _apply(store, STATES_TOPIC, devices({"id": 138, "stan_json": REG_PAYLOAD}))
    assert store.objects[138].thermostat == REG_READBACK


def test_pending_reg_push_replays_thermostat() -> None:
    """A reg push racing ahead of its catalogue row keeps its readback."""
    store = _store()
    _apply(store, f"ampio/fromDB/{USER}/ob/138/state", REG_PAYLOAD)
    _apply(store, DETAILS_TOPIC, details({"id": 138, "typ_komponentu": "reg"}))
    obj = store.objects[138]
    assert obj.value == "0"
    assert obj.thermostat == REG_READBACK


# --- module diagnostics ----------------------------------------------------


def _diag_store() -> AmpioStore:
    """Store that knows module 7 at mac 0xCAFE."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    return store


def test_diagnostics_sets_voltage_and_temperature() -> None:
    store = _diag_store()

    applied = _apply(store, "ampio/from/CAFE/b/4F", '{"d":[254,79,63,142],"m":51966}')

    module = store.modules[7]
    assert module.supply_voltage == 12.6
    assert module.temperature == 42.0
    assert module.last_seen is not None
    assert _mod_updated(applied) == [module]


def test_diagnostics_without_a_temperature_sensor_reports_none() -> None:
    """`0` in the temperature byte marks the sensor as absent, not -100 C."""
    store = _diag_store()
    _apply(store, "ampio/from/CAFE/b/4F", '{"d":[254,79,60,0],"m":51966}')
    module = store.modules[7]
    assert module.supply_voltage == 12.0
    assert module.temperature is None


def test_diagnostics_for_an_unknown_module_is_ignored() -> None:
    store = _diag_store()
    _apply(store, "ampio/from/BEEF/b/4F", '{"d":[254,79,60,0],"m":48879}')
    assert store.modules[7].supply_voltage is None


# --- module catalogue events and event snapshots ---------------------------


def test_devices_reply_dispatches_module_updated_for_new_and_changed() -> None:
    """The module list is news exactly as the object catalogue is: a module
    it adds or changes dispatches ModuleUpdated; an identical re-request
    dispatches nothing."""
    store = _store()
    first = _apply(store, DEVICES_TOPIC, _devices(10, 20))
    assert [m.id for m in _mod_updated(first)] == [1, 2]

    again = _apply(store, DEVICES_TOPIC, _devices(10, 20))
    assert _mod_updated(again) == []

    doc = json.loads(_devices(10, 20))
    doc["List"][1]["nazwa_urzadzenia"] = "renamed"
    changed = _apply(store, DEVICES_TOPIC, json.dumps(doc))
    assert [(m.id, m.name) for m in _mod_updated(changed)] == [(2, "renamed")]


def test_object_updated_carries_a_snapshot() -> None:
    """A dispatched event freezes the state it announced; later changes to
    the same object must not reach a listener that deferred processing.
    Objects are frozen, so the store publishes a new instance per change."""
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 5}))
    state_topic = f"ampio/fromDB/{USER}/ob/5/state"
    (event,) = _updated(_apply(store, state_topic, '{"state": "1", "on": 2000}'))
    _apply(store, state_topic, '{"state": "2", "on": 3000}')
    assert event.value == "1"
    assert store.objects[5].value == "2"


def test_module_updated_carries_a_snapshot() -> None:
    store = _diag_store()
    diag_topic = "ampio/from/CAFE/b/4F"
    (event,) = _mod_updated(_apply(store, diag_topic, '{"d":[254,79,60,110],"m":0}'))
    _apply(store, diag_topic, '{"d":[254,79,70,110],"m":0}')
    assert event.supply_voltage == 12.0
    assert store.modules[7].supply_voltage == 14.0


def test_a_cleared_name_clears_in_the_store() -> None:
    """Every metadata field mirrors the catalogue, name included: an empty
    opis_menu is a normal wire state (unnamed objects), and both discovery
    surfaces agree on names, so a server-side clear must clear here too."""
    store = _store()
    _apply(store, DETAILS_TOPIC, _catalogue(id=9, opis_menu="Old name"))
    assert store.objects[9].name == "Old name"
    applied = _apply(store, DETAILS_TOPIC, _catalogue(id=9, opis_menu=""))
    assert store.objects[9].name is None
    assert [o.id for o in _updated(applied)] == [9]


def test_updated_at_takes_the_report_date_or_the_receipt_time() -> None:
    """A dated report stamps the M-SERV's own `on` (0 is a value, not
    absence); an undated push stamps receipt; an undated seed leaves None."""
    store = _store()
    _apply(store, DETAILS_TOPIC, details({"id": 9}))
    topic = f"ampio/fromDB/{USER}/ob/9/state"
    _apply(store, topic, '{"state": "1", "on": 0}')
    assert store.objects[9].updated_at == 0.0
    before = time.time()
    _apply(store, topic, '{"state": "2"}')
    updated_at = store.objects[9].updated_at
    assert updated_at is not None and before <= updated_at <= time.time()

    seeded = _store()
    _apply(seeded, DETAILS_TOPIC, _flaga_details((9, 1)))
    _apply(
        seeded,
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
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].raw_proven is True
    retyped = dict(_flaga_row(50, 32), typ_komponentu="roleta_procenty")
    _apply(store, DETAILS_TOPIC, details(retyped))
    assert store.objects[50].raw_proven is False


def test_clearing_raw_proven_dispatches_the_final_state() -> None:
    """The flip back to per-object updates is public state; the reply's
    events must end with a snapshot carrying raw_proven False."""
    store = _panel_store()
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    retyped = dict(_flaga_row(50, 32), typ_komponentu="roleta_procenty")
    applied = _apply(store, DETAILS_TOPIC, details(retyped))
    assert store.objects[50].raw_proven is False
    assert any(o.id == 50 and o.raw_proven is False for o in _updated(applied))


def test_a_formerly_raw_proven_value_survives_a_skewed_snapshot() -> None:
    """Clearing raw_proven hands the object back to the per-object path,
    not to a skewed DB seed: the raw value stamped local time, so a dated
    snapshot waits for the next request cycle."""
    store = _panel_store()
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    retyped = dict(_flaga_row(50, 32), typ_komponentu="roleta_procenty")
    _apply(store, DETAILS_TOPIC, details(retyped))
    assert store.objects[50].raw_proven is False
    far_future = int((time.time() + 3600) * 1000)
    stan = json.dumps({"state": "0", "on": far_future})
    _apply(store, STATES_TOPIC, json.dumps({"List": [{"id": 50, "stan_json": stan}]}))
    assert store.objects[50].value == "1"


def test_colliding_override_macs_warn_once_and_surface(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Raw routing is keyed by mac, so a shared override mac is loud: one
    warning when the collision appears, silence while it persists, and a
    cleared set once Designer resolves it."""
    store = _store()
    row_a = {"id": 1, "mac": 0xCAFE, "typ_urzadzenia": 4, "nazwa_urzadzenia": "A"}
    row_b = {"id": 2, "mac": 0xCAFE, "typ_urzadzenia": 4, "nazwa_urzadzenia": "B"}
    with caplog.at_level("WARNING", logger="ampio_mqtt._store"):
        _apply(store, DEVICES_TOPIC, devices(row_a, row_b))
    assert store.colliding_macs == {0xCAFE}
    assert "share the override mac" in caplog.text

    caplog.clear()
    renamed = dict(row_b, nazwa_urzadzenia="B2")
    with caplog.at_level("WARNING", logger="ampio_mqtt._store"):
        _apply(store, DEVICES_TOPIC, devices(row_a, renamed))
    assert "share the override mac" not in caplog.text

    resolved = dict(row_b, mac=0xBEEF)
    _apply(store, DEVICES_TOPIC, devices(row_a, resolved))
    assert store.colliding_macs == frozenset()


# --- Designer-resolved location and matter type -----------------------------


def _seed_catalogue(store: AmpioStore, *rows: dict) -> Applied:
    """Apply a `devicesDetails` catalogue reply carrying `rows` (or none)."""
    return _apply(store, DETAILS_TOPIC, details(*rows))


def test_apply_designer_metadata_sets_location_and_refines_type() -> None:
    store = AmpioStore()
    _seed_catalogue(
        store, {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"}
    )
    applied = store.apply_designer_metadata(
        {64: DesignerResolution(location="Potter", matter_device_type=256)}
    )
    assert store.objects[64].location == "Potter"
    assert store.objects[64].matter_device_type == 256
    assert [e.object.id for e in applied.events] == [64]
    # Re-applying the identical table is not news.
    assert (
        store.apply_designer_metadata(
            {64: DesignerResolution(location="Potter", matter_device_type=256)}
        ).events
        == []
    )


def test_designer_type_never_clears_the_db_column() -> None:
    store = AmpioStore()
    _seed_catalogue(
        store,
        {
            "id": 5,
            "typ_komponentu": "przekaznik",
            "leafId": "0_cb89_257_2_1",
            "type": "266",
        },
    )
    store.apply_designer_metadata(
        {5: DesignerResolution(location="Testowe", matter_device_type=None)}
    )
    assert store.objects[5].matter_device_type == 266
    assert store.objects[5].location == "Testowe"


def test_catalogue_merge_reapplies_the_designer_table() -> None:
    store = AmpioStore()
    row = {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"}
    _seed_catalogue(store, row)
    store.apply_designer_metadata(
        {64: DesignerResolution(location="Potter", matter_device_type=256)}
    )
    _seed_catalogue(store)  # eviction: empty catalogue
    _seed_catalogue(store, row)  # the object returns
    assert store.objects[64].location == "Potter"
    assert store.objects[64].matter_device_type == 256


def test_apply_designer_metadata_for_an_unknown_id_waits_for_the_catalogue() -> None:
    """A resolution racing ahead of the catalogue (or arriving for an object
    just evicted) creates no placeholder - the held table applies it once the
    id's own catalogue row lands, mirroring the params_devices convention
    (`test_params_table_before_catalogue_supplies_hidden_flag`)."""
    store = AmpioStore()
    applied = store.apply_designer_metadata(
        {999: DesignerResolution(location="X", matter_device_type=256)}
    )
    assert applied.events == []
    assert store.objects == {}

    _seed_catalogue(
        store, {"id": 999, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"}
    )
    assert store.objects[999].location == "X"
    assert store.objects[999].matter_device_type == 256


def test_designer_location_none_clears_a_stale_name() -> None:
    store = AmpioStore()
    row = {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"}
    _seed_catalogue(store, row)
    store.apply_designer_metadata(
        {64: DesignerResolution(location="Potter", matter_device_type=None)}
    )
    assert store.objects[64].location == "Potter"

    applied = store.apply_designer_metadata(
        {64: DesignerResolution(location=None, matter_device_type=None)}
    )
    assert store.objects[64].location is None
    assert [e.object.id for e in applied.events] == [64]

    # The clear survives a catalogue re-merge.
    again = _seed_catalogue(store, row)
    assert store.objects[64].location is None
    assert again.events == []


# --- ObjectAdded: an object's first event -----------------------------------


def test_new_catalogue_row_dispatches_object_added() -> None:
    store = AmpioStore()
    applied = _seed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"})
    assert [type(e) for e in applied.events] == [ObjectAdded]
    assert applied.events[0].object.id == 7
    # The same reply again says nothing new.
    assert _seed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"}).events == []


def test_known_row_change_dispatches_updated_not_added() -> None:
    store = AmpioStore()
    _seed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"})
    applied = _seed_catalogue(
        store, {"id": 7, "typ_komponentu": "flaga", "opis_menu": "x"}
    )
    assert [type(e) for e in applied.events] == [ObjectUpdated]


def test_recreation_after_eviction_dispatches_added_again() -> None:
    store = AmpioStore()
    _seed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"})
    removed = _seed_catalogue(store)  # empty catalogue evicts
    assert [type(e) for e in removed.events] == [ObjectRemoved]
    readded = _seed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"})
    assert [type(e) for e in readded.events] == [ObjectAdded]


def test_bare_row_creation_still_dispatches_added() -> None:
    store = AmpioStore()
    applied = _seed_catalogue(store, {"id": 9})
    assert [type(e) for e in applied.events] == [ObjectAdded]
