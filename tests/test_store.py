"""The store applies messages without a client, a broker, or an event loop.

These drive the stores directly, which is the point of them being separate:
protocol behaviour is reachable from a plain function call, and what a message
changed is a return value rather than something to reconstruct from callbacks.
Tests speak in wire topics for readability; :func:`_apply` routes them the
way the client dispatcher does, with the endpoint scoping the client applies
left off, so a test may feed a store any wire shape its class handles.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import fields, replace

import pytest
from conftest import (
    ADMIN_DATA_DEVICES_TOPIC,
    ADMIN_PARAMS_DEVICES_TOPIC,
    ADMIN_USER,
    DATA_DEVICES_TOPIC,
    DEVICES_TOPIC,
    INFO_TOPIC,
    PARAMS_DEVICES_TOPIC,
    STATES_TOPIC,
    USER,
    details,
    devices,
    info,
    params_of,
    params_table,
    snapshot,
)

from ampio_mqtt import (
    ModuleAddress,
    PresenceChanged,
    PresenceDetection,
    PresenceSimulation,
    _protocol,
)
from ampio_mqtt._protocol import (
    ADMIN_ENDPOINTS,
    BASE_ENDPOINTS,
    ENDPOINTS,
    EndpointReply,
    Router,
    decode_envelope,
)
from ampio_mqtt._store import AdminStore, AmpioStore, Applied
from ampio_mqtt.classification import (
    InputKind,
    OutputKind,
    SensorKind,
    ThermostatKind,
)
from ampio_mqtt.errors import AmpioProtocolError
from ampio_mqtt.events import (
    BusEventRaised,
    ModuleRemoved,
    ModuleUpdated,
    NotConfigured,
    ObjectAdded,
    ObjectRemoved,
    ObjectUpdated,
)
from ampio_mqtt.models import (
    AmpioModule,
    AmpioObject,
    CoverParameters,
    DesignerRecord,
    ModuleRecord,
    PanelSettings,
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
    USER: Router(USER, ENDPOINTS, admin=True),
    ADMIN_USER: Router(ADMIN_USER, ENDPOINTS, admin=True),
}


def _apply(
    store: AmpioStore,
    topic: str,
    payload: str,
    *,
    user: str = USER,
    retained: bool = False,
) -> Applied:
    """Route a wire message and apply it, as the client dispatcher does.

    An unroutable topic applies nothing, exactly as the dispatcher drops it.
    ``retained`` marks a broker replay from its retained store.
    """
    msg = _ROUTERS[user].route(topic, payload)
    if msg is None:
        return Applied()
    if isinstance(msg, EndpointReply):
        # The dispatcher decodes a table reply once and hands the store the
        # envelope, so a test that fed the raw bytes would exercise a path
        # the client never takes.
        return store.apply_endpoint(
            msg.endpoint, decode_envelope(msg.payload, msg.endpoint.name)
        )
    return store.apply(msg, retained=retained)


def _store() -> AdminStore:
    """A store for the reserved admin login."""
    return AdminStore()


def _app_store() -> AmpioStore:
    """A store for a standard account."""
    return AmpioStore()


def _feed_catalogue(store: AmpioStore, *items: dict, user: str = USER) -> Applied:
    """One catalogue reply: the params table the rows imply, then the rows.

    The table lands first so every row merges with its config columns in
    hand, and the returned Applied is the catalogue reply's own.
    """
    _apply(store, PARAMS_DEVICES_TOPIC, params_of(*items), user=user)
    return _apply(store, DATA_DEVICES_TOPIC, details(*items), user=user)


def _devices(*macs: int) -> str:
    return devices(
        *(
            {
                "id": i,
                "mac": mac,
                "mac_global": 100 + i,
                "nazwa_urzadzenia": chr(ord("A") + i - 1),
                "typ_urzadzenia": 11,
            }
            for i, mac in enumerate(macs, start=1)
        )
    )


def _module_row(mid: int, mac: int) -> dict:
    """One module-list row, addressed by ``mac`` and identified by ``mid``."""
    return {"id": mid, "mac": mac, "mac_global": 100 + mid}


def _flaga_rows(*object_mac_pairs: tuple[int, int]) -> list[dict]:
    return [
        {
            "id": oid,
            "typ_komponentu": "flaga",
            "interpretacja": 1,
            "funkcja": 3,
            "leafId": f"0_{mac:x}_3_0_2",
            "opis_menu": f"flag-{oid}",
        }
        for oid, mac in object_mac_pairs
    ]


def _catalogue_row(**overrides: object) -> dict:
    return {
        "id": 41,
        "typ_komponentu": "przekaznik",
        "interpretacja": 1,
        "leafId": "0_a_1_0_0",
        "opis_menu": "Lamp",
        "params": 1,
        **overrides,
    }


# A module whose mac is 0xCAFE, so its raw topics are `ampio/from/CAFE/...`.
_PANEL = {"id": 7, "mac": 0xCAFE, "typ_urzadzenia": 11, "nazwa_urzadzenia": "panel"}

# The stored settings of a two-field touch panel, as a sweep reads them.
_PANEL_SETTINGS = PanelSettings(
    touch_field_color=(0, 0, 0, 255),
    status_color=(255, 10, 10),
    light_signal=(1, 1),
    beep_time=2,
    sound_signal=(True, True),
    backlight_active=(True, True),
    multitouch_lock=(False, False),
    multitouch_send_count=False,
    dim_after_s=10,
    dim_brightness=50,
)

# The stored travel configuration of one cover channel, as a sweep reads it.
_COVER_PARAMS = CoverParameters(
    with_slats=False,
    open_time_s=40,
    close_time_s=40,
    calibration_percent=10,
    slat_time_ms=1000,
    reversal_lag_ms=500,
    start_lag_same_ms=None,
    start_lag_other_ms=None,
)


def _flaga_row(oid: int, funkcja: int, mac: int = 0xCAFE) -> dict:
    """One flag row whose leaf embeds the mac of the module it names."""
    return {
        "id": oid,
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "funkcja": funkcja,
        "leafId": f"0_{mac:x}_3_0_{funkcja - 1}",
        "opis_menu": "Flag",
    }


def test_a_catalogue_reply_reports_the_rows_it_changed() -> None:
    applied = _feed_catalogue(_store(), _catalogue_row())
    assert [o.id for o in _updated(applied)] == [41]


def test_an_unchanged_row_reports_nothing() -> None:
    store = _store()
    _feed_catalogue(store, _catalogue_row())
    assert _feed_catalogue(store, _catalogue_row()).events == []


def test_a_changed_row_reports_only_that_row() -> None:
    store = _store()
    _feed_catalogue(store, _catalogue_row())
    applied = _feed_catalogue(store, _catalogue_row(opis_menu="Renamed"))
    assert [o.name for o in _updated(applied)] == ["Renamed"]


def test_an_unreadable_reply_is_refused() -> None:
    """The caller catches this to keep discovery from latching on a bad
    payload, and to report the fault."""
    with pytest.raises(AmpioProtocolError):
        _apply(_store(), DATA_DEVICES_TOPIC, "null")


def test_a_leaf_id_that_is_not_a_string_refuses_the_whole_catalogue_reply() -> None:
    store = _store()
    _feed_catalogue(store, _catalogue_row())
    objects_before = dict(store.objects)
    not_configured_before = store.not_configured
    with pytest.raises(AmpioProtocolError, match="neither a string nor null"):
        _feed_catalogue(store, _catalogue_row(id=42, leafId=123))
    assert store.objects == objects_before
    assert store.not_configured == not_configured_before


@pytest.mark.parametrize(
    ("topic", "payload", "event_type"),
    [
        (
            f"ampio/fromDB/{USER}/ob/41/state",
            '{"state":"1","on":1789000000000}',
            ObjectUpdated,
        ),
        ("ampio/from/1/event", "189", BusEventRaised),
    ],
)
def test_live_messages_dispatch_their_event(
    topic: str, payload: str, event_type: type
) -> None:
    store = _store()
    _feed_catalogue(store, {"id": 41})
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

    DB ids are reassigned when a module is replaced, so a raw-owned flag can
    come back as something else entirely - which no raw channel feeds.
    """
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    original = _flaga_row(50, 32)
    _feed_catalogue(store, original)
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    assert store.objects[50].state == "1"

    # The row keeps its leaf and turns into a cover, which no raw channel
    # feeds, so its only updates become the per-object ones. The leaf stays
    # the same, so the object address does not change either: leaving the
    # index is what frees it, not a moved address.
    _feed_catalogue(
        store,
        {
            "id": 50,
            "typ_komponentu": "roleta_procenty",
            "interpretacja": 1,
            "funkcja": 2,
            "leafId": original["leafId"],
        },
    )
    applied = _apply(
        store, f"ampio/fromDB/{USER}/ob/50/state", '{"state":"55","on":1789000000000}'
    )

    assert [o.id for o in _updated(applied)] == [50]
    assert store.objects[50].state == "55"


def _ledww_row(oid: int, funkcja: int, mac: int = 0xCAFE) -> dict:
    """One CCT row whose leaf embeds the mac of the module it names."""
    return {
        "id": oid,
        "typ_komponentu": "ledww",
        "interpretacja": 1,
        "funkcja": funkcja,
        "leafId": f"0_{mac:x}_30_0_{funkcja - 1}",
        "opis_menu": "CCT",
    }


def test_a_color_temp_broadcast_feeds_every_channel_it_carries() -> None:
    """One frame carries three channels, so each object on the module picks
    up its own pair. The state it lands is the packed u16 the per-object
    topic would have reported, which is what lets `cct` decode either
    source."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _ledww_row(60, 1), _ledww_row(61, 3))
    _apply(
        store,
        "ampio/from/CAFE/b/62",
        json.dumps({"d": [254, 98, 84, 85, 0, 0, 7, 9]}),
    )
    assert store.objects[60].state == str(84 | 85 << 8)
    assert store.objects[60].cct == (84, 85)
    # Channel 2 belongs to no object here, so only channels 1 and 3 land.
    assert store.objects[61].cct == (7, 9)


def test_a_color_temp_broadcast_for_an_unknown_channel_is_dropped() -> None:
    """A live frame for a channel no object exposes is one nothing will ever
    route, exactly as an unmatched raw channel edge is."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _ledww_row(60, 1))
    applied = _apply(store, "ampio/from/CAFE/b/63", json.dumps({"d": [254, 99, 1, 2]}))
    assert _updated(applied) == []
    assert store.objects[60].state is None


def test_the_store_is_the_only_thing_holding_state() -> None:
    store = _store()
    _apply(store, f"ampio/fromDB/{USER}/data/info", info(mac="47846"))
    assert store.server_info is not None and store.server_info.mac == 47846
    assert isinstance(store.objects, dict)
    assert all(isinstance(o, AmpioObject) for o in store.objects.values())


def test_a_below_baseline_server_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    payload = info(serverVersion="409")
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
            info(serverVersion="1865"),
        )
    assert caplog.records == []


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        # A reply without a server mac carries no identity to scope a
        # consumer's registry by, so it is refused like any other.
        '{"Results": {}}',
    ],
)
def test_a_refused_info_reply_never_wipes_held_identity(
    caplog: pytest.LogCaptureFixture, payload: str
) -> None:
    """The discovery latch never clears, so a True wait_for_initial_discovery
    must keep implying a populated identity on every later read. A refused
    info reply must not take it away, and must not trip the below-baseline
    warning off the wiped version."""
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    _apply(store, topic, info(serverVersion="1865"))
    with (
        caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"),
        pytest.raises(AmpioProtocolError),
    ):
        _apply(store, topic, payload)
    assert store.server_info is not None
    assert store.server_info.mac == 1
    assert store.server_info.server_version == "1865"
    assert not any("baseline" in r.getMessage() for r in caplog.records)


def test_the_info_reply_account_id_is_not_checked_against_the_store() -> None:
    """The account id is a wire fact each store records as it reads it."""
    base = _app_store()
    _apply(base, INFO_TOPIC, info(mac=555, userId=-1, serverVersion="1865"))
    assert base.server_info is not None and base.server_info.user_id == -1
    admin = _store()
    _apply(admin, INFO_TOPIC, info(mac=555, userId=4, serverVersion="1865"))
    assert admin.server_info is not None and admin.server_info.user_id == 4


def test_the_base_store_applies_no_raw_message() -> None:
    """The base router routes no raw topic, so one reaching the base store
    is a broken invariant rather than a message to drop."""
    store = _app_store()
    edge = _protocol.RawChannelEdge(mac=0xA, prefix="f", channel=3, state="1")
    with pytest.raises(RuntimeError, match="RawChannelEdge"):
        store.apply(edge)


def test_a_raw_edge_marks_the_object_raw_owned_in_the_admin_store() -> None:
    """The raw path owns the object from its first edge, so the slower
    per-object echo of the same change is dropped."""
    store = _store()
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA))
    _apply(store, "ampio/from/a/state/f/3", "1")
    assert 41 in store._raw_owned
    echoed = _apply(
        store,
        f"ampio/fromDB/{USER}/ob/41/state",
        json.dumps({"state": "0", "on": 1779560000000}),
    )
    assert echoed.events == []
    assert store.objects[41].state == "1"


def test_an_object_leaving_the_index_is_released_without_an_event() -> None:
    """Raw ownership is store bookkeeping, so the release itself changes
    nothing a consumer can read and the row change is the only news."""
    store = _store()
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA))
    _apply(store, "ampio/from/a/state/f/3", "1")
    retyped = {**_flaga_row(41, 3, mac=0xA), "typ_komponentu": "roleta_procenty"}
    applied = _feed_catalogue(store, retyped)
    assert 41 not in store._raw_owned
    assert [obj.id for obj in _updated(applied)] == [41]


def test_below_baseline_warning_survives_an_identityless_reply_arriving_first(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An identity-less reply is never stored, so it cannot pre-seat the
    version and suppress the warning the identified reply should trip."""
    store = _store()
    topic = f"ampio/fromDB/{USER}/data/info"
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        with pytest.raises(AmpioProtocolError):
            _apply(store, topic, '{"Results": {"userId": -1, "serverVersion": "100"}}')
        _apply(store, topic, info(serverVersion="100"))
    warnings = [r for r in caplog.records if "baseline" in r.getMessage()]
    assert len(warnings) == 1
    assert "100" in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("endpoints", "store_class"),
    [("BASE_ENDPOINTS", AmpioStore), ("ADMIN_ENDPOINTS", AdminStore)],
)
def test_handler_table_misalignment_fails_at_construction(
    monkeypatch: pytest.MonkeyPatch, endpoints: str, store_class: type[AmpioStore]
) -> None:
    """A handler-gated endpoint row without a matching store handler (a
    name typo, a row added without its handler) would surface as a silent
    discovery hang; construction refuses it instead. Each store class
    checks its own endpoint set."""
    rogue = _protocol.Endpoint("rogue", "data", "rogue", "data", "rogue")
    monkeypatch.setattr(_protocol, endpoints, (*getattr(_protocol, endpoints), rogue))
    with pytest.raises(RuntimeError, match="rogue"):
        store_class()


def _raw_owned_flag(store: AdminStore, mac: int = 0xCAFE) -> None:
    """Discover one flaga (ob/10 on module 1, channel f/3) and land a raw edge."""
    _apply(store, DEVICES_TOPIC, _devices(mac))
    _feed_catalogue(store, *_flaga_rows((10, mac)))
    _apply(store, f"ampio/from/{mac:X}/state/f/3", "1")


def _snapshot(state: str, on_ms: int | None, oid: int = 10) -> str:
    stan: dict[str, object] = {"state": state}
    if on_ms is not None:
        stan["on"] = on_ms
    return snapshot({"id": oid, "stan_json": json.dumps(stan)})


def test_snapshots_never_touch_a_raw_owned_object() -> None:
    """A raw-owned object's resync is the broker's retained raw table; a
    DB snapshot may be staler than that raw truth with no comparable clock
    to prove it, so its rows are skipped whatever date they carry."""
    store = _store()
    _raw_owned_flag(store)
    far_future = int((time.time() + 7200) * 1000)
    applied = _apply(store, STATES_TOPIC, _snapshot("0", far_future))
    assert _updated(applied) == []
    assert store.objects[10].state == "1"


def test_the_echo_of_a_raw_edge_is_ignored_whole() -> None:
    """The per-object echo repeats what the raw edge delivered ~150 ms
    earlier: no second event, no overwrite, not even its timestamp."""
    store = _store()
    _raw_owned_flag(store)
    before = store.objects[10].updated_at
    applied = _apply(
        store,
        f"ampio/fromDB/{USER}/ob/10/state",
        json.dumps({"state": "255", "on": 1787000000000}),
    )
    assert _updated(applied) == []
    assert store.objects[10].state == "1"
    assert store.objects[10].updated_at == before


def test_a_snapshot_row_with_no_stamp_is_refused() -> None:
    """The stamp is what orders a seed against a live value, so a blob
    without one seeds nothing. The held value stands."""
    store = _store()
    _feed_catalogue(store, {"id": 10})
    _apply(
        store, f"ampio/fromDB/{USER}/ob/10/state", '{"state":"live","on":1789000000000}'
    )
    with pytest.raises(AmpioProtocolError, match="on"):
        _apply(store, STATES_TOPIC, _snapshot("undated", None))
    assert store.objects[10].state == "live"


def test_a_newer_snapshot_corrects_a_value_that_changed_during_an_outage() -> None:
    """The snapshot is the only resync after a reconnect (the per-object
    topics are not retained), so a dated-newer report must overwrite the
    value a pre-outage live push left behind."""
    store = _store()
    _feed_catalogue(store, {"id": 10})
    _apply(
        store, f"ampio/fromDB/{USER}/ob/10/state", '{"state":"255","on":1786700100000}'
    )
    assert store.objects[10].state == "255"

    # Reconnect: the object was switched off while the connection was down.
    applied = _apply(store, STATES_TOPIC, _snapshot("0", 1786700900000))
    assert [o.id for o in _updated(applied)] == [10]
    assert store.objects[10].state == "0"


def test_a_moved_leaf_returns_the_object_to_per_object_updates() -> None:
    store = _store()
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA))
    _apply(store, "ampio/from/a/state/f/3", "1")
    assert 41 in store._raw_owned
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xB))
    assert 41 not in store._raw_owned
    _apply(store, STATES_TOPIC, _snapshot("0", 1779560000000, oid=41))
    assert store.objects[41].state == "0"


def test_begin_refresh_lets_the_snapshot_resync_a_locally_stamped_value() -> None:
    """A raw edge is the one report with no stamp of its own, so its value
    carries this process's clock. A new request cycle proves the next
    snapshot is at least as fresh as anything held, which is what lets the
    seed correct such a value once the object leaves the raw index."""
    store = _store()
    _raw_owned_flag(store)
    # Retyped to a kind the raw tree does not carry, so the object leaves
    # the index and goes back to the per-object path. The leaf stays put,
    # as a Designer retype leaves it, so this is a pure retype and not
    # also a leaf move.
    retyped = {"id": 10, "typ_komponentu": "roleta_procenty", "leafId": "0_cafe_3_0_2"}
    _feed_catalogue(store, retyped)
    assert 10 not in store._raw_owned
    far_future = int((time.time() + 3600) * 1000)
    assert _updated(_apply(store, STATES_TOPIC, _snapshot("stale", far_future))) == []
    store.begin_refresh()
    applied = _apply(store, STATES_TOPIC, _snapshot("0", 1786700900000))
    assert [o.id for o in _updated(applied)] == [10]
    assert store.objects[10].state == "0"


def test_echo_of_an_earlier_edge_does_not_disturb_a_fast_toggle() -> None:
    """Edge 1, edge 2, then the echo of edge 1: the value must stay edge 2's
    and nothing may notify - the echo contributes nothing at all."""
    store = _store()
    _raw_owned_flag(store)
    _apply(store, f"ampio/from/{0xCAFE:X}/state/f/3", "0")  # edge 2
    before = store.objects[10].updated_at
    applied = _apply(
        store,
        f"ampio/fromDB/{USER}/ob/10/state",
        json.dumps({"state": "255", "on": int(time.time() * 1000)}),  # echo of edge 1
    )
    assert _updated(applied) == []
    assert store.objects[10].state == "0"
    assert store.objects[10].updated_at == before


def test_the_config_catalogue_evicts_what_it_stopped_listing() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE, 0xBEEF))
    _feed_catalogue(store, *_flaga_rows((10, 0xCAFE), (11, 0xBEEF)))
    assert set(store.objects) == {10, 11}

    applied = _feed_catalogue(store, *_flaga_rows((10, 0xCAFE)))
    assert [o.id for o in _removed(applied)] == [11]
    assert set(store.objects) == {10}

    # The unchanged catalogue on the next refresh removes nothing further.
    again = _feed_catalogue(store, *_flaga_rows((10, 0xCAFE)))
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
    _feed_catalogue(store, *_flaga_rows((10, 0xCAFE), (11, 0xBEEF)))
    _apply(store, f"ampio/from/{0xBEEF:X}/state/f/3", "1")
    assert store.objects[11].state == "1"

    _feed_catalogue(store, *_flaga_rows((10, 0xCAFE)))
    applied = _apply(store, f"ampio/from/{0xBEEF:X}/state/f/3", "0")
    assert _updated(applied) == []
    assert 11 not in store.objects


def test_a_tier_scoped_router_leaves_the_other_tiers_surfaces_unroutable() -> None:
    """The store treats every catalogue reply as complete for its account
    because the client routes only the tier's served endpoints. The object
    catalogue pair answers both routers, and the restricted router never
    yields the admin-only module list."""
    admin = Router(ADMIN_USER, ADMIN_ENDPOINTS, admin=True)
    restricted = Router(USER, BASE_ENDPOINTS, admin=False)
    assert admin.route(ADMIN_DATA_DEVICES_TOPIC, "{}") is not None
    assert admin.route(ADMIN_PARAMS_DEVICES_TOPIC, "{}") is not None
    assert restricted.route(DATA_DEVICES_TOPIC, "{}") is not None
    assert restricted.route(PARAMS_DEVICES_TOPIC, "{}") is not None
    assert restricted.route(DEVICES_TOPIC, "{}") is None


def test_the_app_sync_catalogue_evicts_what_the_grant_revoked() -> None:
    # The grant bounds a restricted store, so the reply is complete for
    # the account and a vanished row is a revocation.
    store = _app_store()
    _feed_catalogue(store, *_flaga_rows((10, 0xCAFE), (11, 0xCAFE)))
    applied = _feed_catalogue(store, *_flaga_rows((10, 0xCAFE)))
    assert [o.id for o in _removed(applied)] == [11]
    assert set(store.objects) == {10}


def test_an_empty_catalogue_reply_evicts_everything() -> None:
    """An empty reply is a complete reply listing nothing - a full grant
    revocation on the app-sync surface, a wiped configuration on config -
    and evicts like any other, one removal event per object and module."""
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE))
    _feed_catalogue(store, *_flaga_rows((10, 0xCAFE)))
    details_applied = _feed_catalogue(store)
    devices_applied = _apply(store, DEVICES_TOPIC, devices())
    assert [o.id for o in _removed(details_applied)] == [10]
    assert [m.id for m in _mod_removed(devices_applied)] == [1]
    assert store.objects == {} and store.modules == {}


def test_live_messages_touch_last_seen_snapshots_do_not() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE))
    _feed_catalogue(store, *_flaga_rows((10, 0xCAFE)))
    assert store.modules[1].last_seen is None

    _apply(store, STATES_TOPIC, _snapshot("1", 1779560000000))
    assert store.modules[1].last_seen is None

    before = time.time()
    _apply(store, f"ampio/from/{0xCAFE:X}/state/f/3", "1")
    seen = store.modules[1].last_seen
    assert seen is not None and before <= seen <= time.time()


def test_a_retained_raw_replay_sets_the_value_but_not_last_seen() -> None:
    """The broker replays the raw table on every subscribe with the retain
    flag set. The value lands and the object becomes raw-owned, but a replay
    is stored state of unknown age, not evidence that the module is alive."""
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xCAFE))
    _feed_catalogue(store, *_flaga_rows((10, 0xCAFE)))

    _apply(store, f"ampio/from/{0xCAFE:X}/state/f/3", "1", retained=True)
    assert store.objects[10].state == "1"
    assert 10 in store._raw_owned
    assert store.modules[1].last_seen is None

    _apply(store, f"ampio/from/{0xCAFE:X}/state/f/3", "0")
    assert store.modules[1].last_seen is not None


def test_a_raw_edge_routes_to_every_object_sharing_the_leaf() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA))
    shared = {
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "funkcja": 3,
        "leafId": "0_a_3_0_2",
    }
    _feed_catalogue(store, {"id": 41, **shared}, {"id": 42, **shared})
    applied = _apply(store, "ampio/from/a/state/f/3", "1")
    assert sorted(o.id for o in _updated(applied)) == [41, 42]
    assert {41, 42} <= store._raw_owned


def test_the_routing_index_keys_on_the_leaf_mac_without_the_module_list() -> None:
    store = _store()
    _feed_catalogue(
        store,
        {
            "id": 41,
            "typ_komponentu": "flaga",
            "interpretacja": 1,
            "funkcja": 3,
            "leafId": "0_a_3_0_2",
        },
    )
    applied = _apply(store, "ampio/from/a/state/f/3", "1")
    assert [o.id for o in _updated(applied)] == [41]


def test_a_live_edge_touches_the_module_on_the_leaf_mac() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB))
    _feed_catalogue(
        store,
        {
            "id": 41,
            "typ_komponentu": "flaga",
            "interpretacja": 1,
            "funkcja": 3,
            "leafId": "0_b_3_0_2",
        },
    )
    _apply(store, "ampio/from/b/state/f/3", "1")
    assert store.modules[1].last_seen is None
    assert store.modules[2].last_seen is not None


def test_module_by_mac_reads_none_for_an_unlisted_mac() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB))
    assert store.module_by_mac(0xA) is not None
    assert store.module_by_mac(0xC) is None


# --- catalogues, state pushes, and snapshots --------------------------------


def test_details_populate_and_classify() -> None:
    store = _store()
    _feed_catalogue(
        store,
        {
            "id": 41,
            "typ_komponentu": "temp",
            "interpretacja": 1,
            "opis_menu": "Salon",
        },
        {
            "id": 107,
            "typ_komponentu": "lin_wej",
            "interpretacja": 7,
            "opis_menu": "CO2",
        },
        {
            "id": 1,
            "typ_komponentu": "przekaznik",
            "interpretacja": 1,
            "opis_menu": "Pump",
            "type": "266",  # 0x010A On/Off Plug-in Unit
        },
    )

    assert set(store.objects) == {41, 107, 1}
    temp = store.objects[41]
    assert temp.kind is not None and temp.kind.device_class == "temperature"
    assert temp.name == "Salon"
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


def test_two_catalogue_rows_sharing_one_leaf_id_get_distinct_object_keys() -> None:
    """Two Designer views of one output share one `leafId`. Both objects
    exist, `leaf_key` reads the same for both, and `object_key` (built
    from the catalogue `id`) still tells them apart."""
    store = _store()
    _feed_catalogue(
        store,
        {
            "id": 150,
            "typ_komponentu": "flaga",
            "leafId": "0_be82_257_2_2",
            "opis_menu": "Relay",
        },
        {
            "id": 151,
            "typ_komponentu": "flaga",
            "leafId": "0_be82_257_2_2",
            "opis_menu": "Relay",
        },
    )
    assert set(store.objects) == {150, 151}
    first, second = store.objects[150], store.objects[151]
    assert first.leaf_key == second.leaf_key == "leaf_0_be82_257_2_2"
    assert first.object_key != second.object_key


# --- the door: one admission path on both tiers -----------------------------


def _not_configured(applied: Applied) -> list[NotConfigured]:
    return [e for e in applied.events if isinstance(e, NotConfigured)]


def test_the_door_waits_for_both_replies_of_the_pair() -> None:
    store = _app_store()
    first = _apply(store, DATA_DEVICES_TOPIC, details({"id": 41}))
    assert first.events == []
    assert store.objects == {}
    second = _apply(store, PARAMS_DEVICES_TOPIC, params_of({"id": 41}))
    assert [type(e) for e in second.events] == [ObjectAdded]
    assert 41 in store.objects


def test_the_door_admits_when_the_params_table_lands_first() -> None:
    store = _store()
    assert _apply(store, PARAMS_DEVICES_TOPIC, params_of({"id": 41})).events == []
    applied = _apply(store, DATA_DEVICES_TOPIC, details({"id": 41}))
    assert [type(e) for e in applied.events] == [ObjectAdded]


def test_a_params_push_alone_re_runs_the_door() -> None:
    store = _store()
    _feed_catalogue(store, {"id": 41})
    hidden = _apply(store, PARAMS_DEVICES_TOPIC, params_of({"id": 41, "params": 16}))
    assert [type(e) for e in hidden.events] == [ObjectRemoved]
    assert 41 not in store.objects
    shown = _apply(store, PARAMS_DEVICES_TOPIC, params_of({"id": 41}))
    assert [type(e) for e in shown.events] == [ObjectAdded]


def test_a_params_push_keeps_the_buffered_push_for_the_next_catalogue() -> None:
    """Only a fresh `data/devices` reply proves an id will never gain a row.

    The params reply of a pair re-runs the door on the held, older
    catalogue, which does not list an object the pair is about to
    establish. A push that raced ahead of the pair must survive that run.
    """
    store = _store()
    _feed_catalogue(store, {"id": 41})
    _apply(store, f"ampio/fromDB/{USER}/ob/70/state", _push(70, "255"))
    _feed_catalogue(store, {"id": 41}, {"id": 70})
    assert store.objects[70].state == "255"

    # An id the fresh reply does not list is one nothing will establish.
    _apply(store, f"ampio/fromDB/{USER}/ob/71/state", _push(71, "1"))
    _feed_catalogue(store, {"id": 41}, {"id": 70})
    assert 71 not in store._pending_state


def test_a_hidden_row_never_enters_objects_nor_the_rejected_set() -> None:
    store = _store()
    applied = _feed_catalogue(store, {"id": 41, "params": 16, "leafId": ""}, {"id": 42})
    assert [e.object.id for e in applied.events if isinstance(e, ObjectAdded)] == [42]
    assert 41 not in store.objects
    assert store.not_configured == ()
    assert _not_configured(applied) == []


def test_an_empty_leaf_is_recorded_and_left_out() -> None:
    store = _store()
    applied = _feed_catalogue(
        store, {"id": 41, "leafId": "", "opis_menu": "Lamp"}, {"id": 42}
    )
    assert list(store.objects) == [42]
    assert store.not_configured == ((41, "Lamp"),)
    assert [e.objects for e in _not_configured(applied)] == [((41, "Lamp"),)]


def test_a_malformed_leaf_refuses_the_reply_whole() -> None:
    store = _store()
    _feed_catalogue(store, {"id": 42}, _DET)
    before = dict(store.objects)
    detection = store.presence_detection
    assert detection is not None
    with pytest.raises(AmpioProtocolError, match="garbage"):
        _feed_catalogue(store, {"id": 41, "leafId": "garbage"}, {"id": 43}, _DET)
    assert store.objects == before
    assert store.not_configured == ()
    assert store.presence_detection == detection
    assert store.presence_simulation is None


def test_a_leaf_that_disappears_after_admission_evicts_the_row() -> None:
    store = _store()
    _feed_catalogue(store, {"id": 41, "typ_komponentu": "przekaznik"})
    _sweep(store, {0xCAFE}, records={41: DesignerRecord(location="Hall")})
    assert 41 in store.records
    applied = _feed_catalogue(store, {"id": 41, "leafId": ""})
    assert [type(e) for e in applied.events] == [ObjectRemoved, NotConfigured]
    assert store.not_configured == ((41, None),)
    restored = _feed_catalogue(store, {"id": 41, "typ_komponentu": "przekaznik"})
    assert [type(e) for e in restored.events] == [ObjectAdded]
    assert store.not_configured == ()
    assert 41 not in store.records


def test_not_configured_reports_a_change_of_the_rejected_set_only() -> None:
    store = _store()
    first = _feed_catalogue(store, {"id": 41, "leafId": ""})
    again = _feed_catalogue(store, {"id": 41, "leafId": ""})
    assert len(_not_configured(first)) == 1
    assert _not_configured(again) == []


def test_a_refused_catalogue_leaves_the_held_reply_untouched() -> None:
    store = _store()
    _feed_catalogue(store, {"id": 41})
    with pytest.raises(AmpioProtocolError):
        _apply(
            store,
            DATA_DEVICES_TOPIC,
            details({"id": 41}, {"id": 43, "leafId": "garbage"}),
        )
    again = _apply(store, PARAMS_DEVICES_TOPIC, params_of({"id": 41}))
    assert again.events == []
    assert list(store.objects) == [41]


def test_a_malformed_snapshot_row_refuses_the_snapshot_whole() -> None:
    store = _store()
    _feed_catalogue(store, {"id": 41}, {"id": 42})
    _apply(store, STATES_TOPIC, _snapshot("7", 1779560000000, oid=41))
    with pytest.raises(AmpioProtocolError):
        _apply(
            store,
            STATES_TOPIC,
            snapshot(
                {
                    "id": 41,
                    "stan_json": json.dumps({"state": "9", "on": 1779560100000}),
                },
                {"id": 42, "stan_json": "not json"},
            ),
        )
    assert store.objects[41].state == "7"
    assert list(store._stan_by_id) == [41]


def test_not_configured_ignores_the_order_of_the_rejected_rows() -> None:
    store = _store()
    first = _feed_catalogue(store, {"id": 41, "leafId": ""}, {"id": 42, "leafId": ""})
    second = _feed_catalogue(store, {"id": 42, "leafId": ""}, {"id": 41, "leafId": ""})
    assert len(_not_configured(first)) == 1
    assert _not_configured(second) == []
    assert store.not_configured == ((41, None), (42, None))


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
    assert mod.nazwa_urzadzenia == "m-sens salon"
    assert mod.typ_urzadzenia == 44
    assert mod.model == "M-SENS"
    assert mod.wersja_softu == 63
    assert mod.wersja_pcb == 7
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
    _feed_catalogue(
        store,
        {
            "id": 41,
            "typ_komponentu": "temp",
            "interpretacja": 1,
            "opis_menu": "T",
            "leafId": "0_1_74_0_0",
        },
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
    _feed_catalogue(
        store,
        {
            "id": 41,
            "typ_komponentu": "temp",
            "interpretacja": 1,
            "opis_menu": "T",
            "leafId": "0_1_74_0_0",
        },
    )
    assert store.objects[41].state is None
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
    assert store.objects[41].state == "22.5"
    assert store.objects[41].updated_at == 1779560000.0
    assert store.modules[17].last_seen is None


def test_states_snapshot_does_not_overwrite_live_value() -> None:
    """A snapshot does not regress a value already set by a live push."""
    store = _store()
    _feed_catalogue(
        store,
        {"id": 41, "typ_komponentu": "temp", "interpretacja": 1, "opis_menu": "T"},
    )
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/41/state",
        '{"state": "fresh", "on": 1779570000000}',
    )
    assert store.objects[41].state == "fresh"

    _apply(
        store,
        STATES_TOPIC,
        devices({"id": 41, "stan_json": '{"state": "stale", "on": 1779560000000}'}),
    )
    assert store.objects[41].state == "fresh"


def test_states_snapshot_creates_nothing_for_unknown_ids() -> None:
    """Only the catalogues decide which objects exist. The snapshot replays
    DB rows, unlisted ids included - creating from it would later evict an
    object no consumer was ever told existed."""
    store = _app_store()
    _feed_catalogue(store, {"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"})
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
    store = _app_store()
    applied = _apply(
        store,
        STATES_TOPIC,
        devices({"id": 20, "stan_json": '{"state": "7", "on": 1779560000000}'}),
    )
    assert 20 not in store.objects
    assert _updated(applied) == []

    applied = _feed_catalogue(
        store, {"id": 20, "typ_komponentu": "temp", "opis_menu": "T"}
    )
    assert [o.id for o in _updated(applied)] == [20]
    assert store.objects[20].state == "7"
    assert store.objects[20].updated_at == 1779560000.0
    assert store.objects[20].name == "T"


def test_eviction_prunes_the_buffered_snapshot_value() -> None:
    """An evicted object's buffered seed must not resurface if a later
    catalogue re-establishes the id."""
    store = _app_store()
    _feed_catalogue(
        store,
        {"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"},
        {"id": 6, "typ_komponentu": "flaga", "opis_menu": "G"},
    )
    _apply(
        store,
        STATES_TOPIC,
        devices(
            {"id": 5, "stan_json": '{"state": "1", "on": 1779560000000}'},
            {"id": 6, "stan_json": '{"state": "1", "on": 1779560000000}'},
        ),
    )
    applied = _feed_catalogue(
        store, {"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"}
    )
    assert [o.id for o in _removed(applied)] == [6]

    _feed_catalogue(
        store,
        {"id": 5, "typ_komponentu": "flaga", "opis_menu": "F"},
        {"id": 6, "typ_komponentu": "flaga", "opis_menu": "G"},
    )
    assert store.objects[6].state is None


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
    assert store.modules[17].nazwa_urzadzenia == "m2"
    assert store.modules[17].last_seen == 1700000000.0


def test_a_push_for_an_uncatalogued_id_waits_for_its_catalogue_row() -> None:
    """Only the catalogues decide which objects exist. A push that races
    ahead of them dispatches nothing and creates nothing; the catalogue
    row surfaces the object already carrying the buffered value, so there
    is no update/remove churn around a catalogue reply."""
    store = _store()
    state_topic = f"ampio/fromDB/{USER}/ob/93/state"
    applied = _apply(
        store, state_topic, '{"state":"187.6","desc":"187.6 ","on":1789000000000}'
    )
    assert applied.events == []
    assert 93 not in store.objects

    applied = _feed_catalogue(store, {"id": 93})
    obj = store.objects[93]
    assert isinstance(obj.kind, SensorKind)  # no typ_komponentu -> fallback
    assert obj.state == "187.6"
    assert [o.state for o in _updated(applied)] == ["187.6"]


def test_a_buffered_push_loses_to_a_newer_dated_snapshot_seed() -> None:
    """The catalogue merge replays the buffered push and the buffered
    snapshot value under one dated-supersedes rule: the fresher wins."""
    store = _store()
    state_topic = f"ampio/fromDB/{USER}/ob/93/state"
    _apply(store, state_topic, '{"state":"old","on":1000}')
    _apply(store, STATES_TOPIC, _snapshot("new", 2000, oid=93))
    _feed_catalogue(store, {"id": 93})
    assert store.objects[93].state == "new"

    fresh = _store()
    _apply(fresh, state_topic, '{"state":"newer","on":3000}')
    _apply(fresh, STATES_TOPIC, _snapshot("new", 2000, oid=93))
    _feed_catalogue(fresh, {"id": 93})
    assert fresh.objects[93].state == "newer"


def test_a_buffered_push_for_an_unlisted_id_is_pruned() -> None:
    """A catalogue that does not list the pushed id proves it will never
    gain a row; the buffered value must not resurface if the id later
    appears (a DB id reassignment, not the same object)."""
    store = _store()
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/99/state",
        '{"state":"ghost","on":1789000000000}',
    )
    _feed_catalogue(store, {"id": 41})
    _feed_catalogue(store, {"id": 41}, {"id": 99})
    assert store.objects[99].state is None


@pytest.mark.parametrize(
    ("make_store", "topic_suffix"),
    [
        (_store, "data/devices"),
        (_store, "data/params_devices"),
        (_store, "config/devices"),
        (_store, "data/states"),
        (_app_store, "data/devices"),
        (_app_store, "data/params_devices"),
        (_app_store, "data/states"),
    ],
)
def test_every_handler_refuses_an_unparseable_payload(
    make_store: Callable[[], AmpioStore], topic_suffix: str
) -> None:
    """No handler reads a reply it cannot trust. The caller reports the
    refusal and drops the message."""
    with pytest.raises(AmpioProtocolError):
        _apply(make_store(), f"ampio/fromDB/{USER}/{topic_suffix}", "not json")


def test_the_base_store_refuses_the_admin_only_module_list() -> None:
    """The module list answers the reserved admin login alone, so the base
    store's handler table carries no entry for it, and an attempt to apply
    one is a routing fault to report."""
    store = _app_store()
    with pytest.raises(RuntimeError, match="has no handler"):
        _apply(store, DEVICES_TOPIC, devices({"id": 24}))


def test_state_with_unparseable_payload_is_dropped() -> None:
    """An `/ob/<non-int>/state` topic is rejected without raising."""
    store = _store()
    _apply(store, f"ampio/fromDB/{USER}/ob/not-an-int/state", "x")
    assert store.objects == {}


def test_stan_json_with_no_state_field_does_not_overwrite_value() -> None:
    """A stan_json blob without `state` should not clobber an existing value."""
    store = _store()
    _feed_catalogue(
        store,
        {"id": 41, "typ_komponentu": "temp", "interpretacja": 1, "opis_menu": "T"},
    )
    _apply(store, STATES_TOPIC, _snapshot("22.5", 1779560000000, oid=41))
    assert store.objects[41].state == "22.5"

    with pytest.raises(AmpioProtocolError, match="state"):
        _apply(
            store,
            STATES_TOPIC,
            snapshot({"id": 41, "stan_json": '{"on": 1779560100000}'}),
        )
    assert store.objects[41].state == "22.5"


def test_numeric_value_none_for_bare_nan_state_push() -> None:
    """A bare NaN literal parses (Python's json accepts it) but reads as None."""
    store = _store()
    _feed_catalogue(store, {"id": 12})
    _apply(
        store, f"ampio/fromDB/{USER}/ob/12/state", '{"state": NaN, "on": 1789000000000}'
    )
    obj = store.objects[12]
    assert obj.state == "nan"
    assert obj.numeric_value is None


# --- raw-channel input bridge ---------------------------------------------


def _panel_store() -> AdminStore:
    """Store that knows panel module 7 (mac CAFE) and a flaga at funkcja 32."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _flaga_row(50, 32))
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
    assert obj.state == "1" and obj.is_on is True
    assert _updated(applied) == [obj]


def test_raw_channel_unmapped_is_ignored() -> None:
    store = _panel_store()

    # funkcja 5 has no object; a different module mac has no objects at all.
    unmapped = _apply(store, "ampio/from/CAFE/state/f/5", "1")
    other_mac = _apply(store, "ampio/from/BEEF/state/f/32", "1")

    assert store.objects[50].state is None
    assert _updated(unmapped) == [] and _updated(other_mac) == []


def test_raw_channel_malformed_topic_is_ignored() -> None:
    """A topic that passes the dispatch filter but fails the parser is dropped."""
    store = _panel_store()
    _apply(store, "ampio/from/CAFE/state/f", "1")  # too short
    assert store.objects[50].state is None


def test_mapped_input_without_raw_uses_per_object_fallback() -> None:
    """A mapped input that never produced a raw edge still updates per-object."""
    store = _panel_store()

    applied = _apply(
        store, f"ampio/fromDB/{USER}/ob/50/state", '{"state": "255", "on": 1700}'
    )
    obj = store.objects[50]
    assert obj.state == "255" and obj.is_on is True
    assert _updated(applied) == [obj]


def test_wej_routes_via_digital_input_prefix() -> None:
    """A physical-input object (#117) bridges on `i/<funkcja>`."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    wej = {
        "id": 62,
        "typ_komponentu": "wej",
        "interpretacja": 1,
        "funkcja": 1,
        "opis_menu": "Button",
    }
    _feed_catalogue(store, wej)
    obj = store.objects[62]
    assert isinstance(obj.kind, InputKind)
    assert obj.kind.key == "wej" and obj.kind.device_class is None
    _apply(store, "ampio/from/CAFE/state/i/1", "1")
    assert store.objects[62].state == "1" and store.objects[62].is_on is True


def test_wej_per_object_edge_reads_255_as_on() -> None:
    """The per-object path (both tiers) publishes 255 pressed / 0 released."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    wej = {
        "id": 63,
        "typ_komponentu": "wej",
        "interpretacja": 1,
        "funkcja": 2,
        "opis_menu": "Button",
    }
    _feed_catalogue(store, wej)
    _apply(store, f"ampio/fromDB/{USER}/ob/63/state", '{"state": "255", "on": 1700}')
    assert store.objects[63].is_on is True
    _apply(store, f"ampio/fromDB/{USER}/ob/63/state", '{"state": "0", "on": 1701}')
    assert store.objects[63].is_on is False


# --- the two presence rows ---------------------------------------------------

_DET = {
    "id": 60,
    "typ_komponentu": "detekcja",
    "interpretacja": 1,
    "funkcja": 1,
    "opis_menu": "Detection",
}
_SIM = {
    "id": 61,
    "typ_komponentu": "symulacja",
    "interpretacja": 1,
    "funkcja": 1,
    "opis_menu": "Simulation",
    "czas": 1,
}
_WEJ = {
    "id": 62,
    "typ_komponentu": "wej",
    "interpretacja": 1,
    "funkcja": 1,
    "opis_menu": "Button",
}
_FLAG = {
    "id": 63,
    "typ_komponentu": "flaga",
    "interpretacja": 1,
    "funkcja": 1,
    "opis_menu": "Flag",
}


def _presence_events(applied: Applied) -> list[PresenceChanged]:
    return [e for e in applied.events if isinstance(e, PresenceChanged)]


def test_presence_rows_leave_the_object_catalogue_on_the_admin_tier() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    applied = _feed_catalogue(store, _WEJ, _DET, _SIM)
    assert set(store.objects) == {62}
    assert store.presence_detection == PresenceDetection(
        id=60, name="Detection", home_status=None
    )
    assert store.presence_simulation == PresenceSimulation(
        id=61, name="Simulation", active=True
    )
    assert _presence_events(applied) == [
        PresenceChanged(
            detection=store.presence_detection,
            simulation=store.presence_simulation,
        )
    ]


def test_presence_rows_leave_the_object_catalogue_on_the_app_sync_tier() -> None:
    store = _app_store()
    applied_params = _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        params_table(
            {"id": 60, "params": 1, "czas": 0},
            {"id": 61, "params": 1, "czas": 0},
            {"id": 62, "params": 0, "czas": 0},
        ),
    )
    applied_catalogue = _apply(store, DATA_DEVICES_TOPIC, details(_WEJ, _DET, _SIM))
    assert set(store.objects) == {62}
    assert store.presence_detection is not None
    assert store.presence_detection.id == 60
    assert store.presence_simulation == PresenceSimulation(
        id=61, name="Simulation", active=False
    )
    # The table alone establishes nothing: the door waits for the reply
    # that lists the rows.
    assert _presence_events(applied_params) == []
    # The catalogue reply settles both rows and reports one event.
    assert _presence_events(applied_catalogue) == [
        PresenceChanged(
            detection=store.presence_detection,
            simulation=store.presence_simulation,
        )
    ]


def test_hidden_presence_row_reads_none() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET, {**_SIM, "params": 16})
    assert store.presence_detection is not None
    assert store.presence_simulation is None


def test_raw_input_edge_never_reaches_the_presence_detection_row() -> None:
    """The detection row shares the M-SERV's module and channel 1 with a
    physical input, and the raw edge belongs to the input alone."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _WEJ, _DET)
    applied = _apply(store, "ampio/from/CAFE/state/i/1", "1")
    assert store.objects[62].state == "1"
    assert store.presence_detection is not None
    assert store.presence_detection.home_status is None
    assert _presence_events(applied) == []


def test_repeated_catalogue_emits_no_presence_change() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET, _SIM)
    applied = _feed_catalogue(store, _DET, _SIM)
    assert _presence_events(applied) == []


def test_presence_rows_evict_when_the_catalogue_stops_listing_them() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET, _SIM)
    applied = _feed_catalogue(store, _WEJ)
    assert store.presence_detection is None
    assert store.presence_simulation is None
    assert _presence_events(applied) == [
        PresenceChanged(detection=None, simulation=None)
    ]


def test_presence_rows_never_enter_the_raw_index() -> None:
    """A `flaga` row shares the M-SERV's module and channel 1 with both
    presence rows, and a raw `f/1` edge belongs to the flag alone."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _FLAG, _DET, _SIM)
    applied = _apply(store, "ampio/from/CAFE/state/f/1", "1")
    assert store.objects[63].state == "1"
    assert _updated(applied) == [store.objects[63]]
    assert _presence_events(applied) == []
    assert store.presence_detection == PresenceDetection(
        id=60, name="Detection", home_status=None
    )
    assert store.presence_simulation == PresenceSimulation(
        id=61, name="Simulation", active=True
    )


def _app_with_presence() -> AmpioStore:
    store = _app_store()
    _apply(store, DATA_DEVICES_TOPIC, details(_DET, _SIM))
    _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        params_table(
            {"id": 60, "params": 1, "czas": 0}, {"id": 61, "params": 1, "czas": 0}
        ),
    )
    return store


def test_params_push_flips_the_simulation_switch_and_reports_presence() -> None:
    store = _app_with_presence()
    applied = _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        params_table(
            {"id": 60, "params": 1, "czas": 0}, {"id": 61, "params": 1, "czas": 1}
        ),
    )
    assert store.presence_simulation == PresenceSimulation(
        id=61, name="Simulation", active=True
    )
    assert _presence_events(applied) == [
        PresenceChanged(
            detection=store.presence_detection, simulation=store.presence_simulation
        )
    ]


def test_params_push_that_hides_the_simulation_row_reads_none_and_reports_presence() -> (
    None
):
    store = _app_with_presence()
    applied = _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        params_table(
            {"id": 60, "params": 1, "czas": 0}, {"id": 61, "params": 17, "czas": 0}
        ),
    )
    assert store.presence_simulation is None
    assert len(_presence_events(applied)) == 1


def test_repeated_params_push_emits_no_presence_change() -> None:
    store = _app_with_presence()
    applied = _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        params_table(
            {"id": 60, "params": 1, "czas": 0}, {"id": 61, "params": 1, "czas": 0}
        ),
    )
    assert _presence_events(applied) == []


def _push(oid: int, state: str, on: int = 1_700_000_000_000) -> str:
    return json.dumps({"state": state, "on": on})


def test_detection_push_sets_the_home_status() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET, _SIM)
    applied = _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "5"))
    assert store.presence_detection is not None
    assert store.presence_detection.home_status == 5
    assert _presence_events(applied) == [
        PresenceChanged(
            detection=store.presence_detection,
            simulation=store.presence_simulation,
        )
    ]
    _feed_catalogue(store, _DET, _SIM)
    assert store.presence_detection.home_status == 5


def test_snapshot_seeds_the_home_status_once() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET)
    _apply(store, STATES_TOPIC, snapshot({"id": 60, "stan_json": _push(60, "5")}))
    assert store.presence_detection is not None
    assert store.presence_detection.home_status == 5
    _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "7"))
    _apply(store, STATES_TOPIC, snapshot({"id": 60, "stan_json": _push(60, "5")}))
    assert store.presence_detection.home_status == 7


def test_snapshot_before_the_catalogue_seeds_the_detection_code() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _apply(store, STATES_TOPIC, snapshot({"id": 60, "stan_json": _push(60, "5")}))
    _feed_catalogue(store, _DET)
    assert store.presence_detection is not None
    assert store.presence_detection.home_status == 5


def test_detection_push_before_the_catalogue_is_replayed_at_the_merge() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "5"))
    _feed_catalogue(store, _DET)
    assert store.presence_detection is not None
    assert store.presence_detection.home_status == 5


def test_detection_code_that_is_not_an_integer_is_a_protocol_fault() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET)
    with pytest.raises(AmpioProtocolError, match="home-status"):
        _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "home"))


def test_simulation_push_changes_nothing() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _SIM)
    before = store.presence_simulation
    applied = _apply(store, f"ampio/fromDB/{USER}/ob/61/state", _push(61, "1"))
    assert store.presence_simulation == before
    assert applied.events == []


def test_malformed_buffered_detection_code_leaves_the_catalogue_unapplied() -> None:
    store = _store()
    _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "home"))
    with pytest.raises(AmpioProtocolError):
        _feed_catalogue(store, _WEJ, _DET)
    assert store.objects == {}
    assert store.presence_detection is None


def test_snapshot_after_a_refresh_corrects_the_detection_code() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET)
    _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "5"))
    store.begin_refresh()
    _apply(store, STATES_TOPIC, snapshot({"id": 60, "stan_json": _push(60, "7")}))
    assert store.presence_detection is not None
    assert store.presence_detection.home_status == 7


def test_snapshot_after_a_refresh_loses_to_a_push_in_the_same_cycle() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET)
    _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "5"))
    store.begin_refresh()
    _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "8"))
    _apply(store, STATES_TOPIC, snapshot({"id": 60, "stan_json": _push(60, "7")}))
    assert store.presence_detection is not None
    assert store.presence_detection.home_status == 8


def test_hidden_detection_row_keeps_its_live_code() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, {**_DET, "params": 16})
    _apply(store, f"ampio/fromDB/{USER}/ob/60/state", _push(60, "7"))
    # The params table is what un-hides the row; feeding it alone isolates
    # the one presence event that reply causes.
    applied = _apply(store, PARAMS_DEVICES_TOPIC, params_of(_DET))
    assert store.presence_detection is not None
    assert store.presence_detection.home_status == 7
    assert len(_presence_events(applied)) == 1


def test_removed_detection_row_returns_without_its_old_code() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _DET)
    _apply(store, STATES_TOPIC, snapshot({"id": 60, "stan_json": _push(60, "5")}))
    assert store.presence_detection is not None
    assert store.presence_detection.home_status == 5
    _feed_catalogue(store, _WEJ)
    assert store.presence_detection is None
    _feed_catalogue(store, _DET)
    assert store.presence_detection is not None
    assert store.presence_detection.home_status is None


# --- the app-sync data surface (standard accounts) --------------------------


def _app_row(oid: int, leaf: str, name: str = "Air quality", interp: int = 5) -> dict:
    """One `data/devices` row: the catalogue shape minus the config columns."""
    return {
        "id": oid,
        "typ_komponentu": "lin_wej",
        "interpretacja": interp,
        "funkcja": 5,
        "leafId": leaf,
        "opis_menu": name,
    }


def test_data_devices_populate_and_classify() -> None:
    store = _app_store()
    _feed_catalogue(store, _app_row(24, "0_cb9b_74_0_1", interp=7))
    obj = store.objects[24]
    assert obj.name == "Air quality"
    assert obj.kind is not None and obj.kind.device_class == "carbon_dioxide"
    assert obj.funkcja == 5
    assert obj.address == ModuleAddress(mac=0xCB9B, channel=1, sf_id=74, sub_sf_id=0)


def test_the_config_table_before_the_catalogue_applies_at_the_merge() -> None:
    """`data/params_devices` is the app-sync tier's one source for the
    Designer config columns, and the two replies arrive in no fixed order.
    A table that lands first is held and applied when the catalogue lands."""
    store = _app_store()
    _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        params_table(
            {"id": 24, "params": 17},
            {"id": 25, "params": 1, "czas": 500, "url": "IAQ"},
        ),
    )
    # The table is not grant-filtered; unknown ids create no placeholders.
    assert store.objects == {}

    _apply(
        store,
        DATA_DEVICES_TOPIC,
        details(_app_row(24, "0_cb9b_74_0_1"), _app_row(25, "0_cb9b_74_0_2")),
    )
    # The held table decides the hidden bit too, so row 24 never lands.
    assert 24 not in store.objects
    assert store.objects[25].czas == 500 and store.objects[25].url == "IAQ"


def test_the_config_table_after_the_catalogue_updates_objects_and_notifies() -> None:
    store = _app_store()
    _apply(store, PARAMS_DEVICES_TOPIC, params_table({"id": 24, "params": 1}))
    _apply(store, DATA_DEVICES_TOPIC, details(_app_row(24, "0_cb9b_74_0_1")))
    assert store.objects[24].czas == 0 and store.objects[24].url == ""

    applied = _apply(
        store,
        PARAMS_DEVICES_TOPIC,
        params_table(
            {"id": 24, "params": 1, "czas": 500, "url": "IAQ"},
            {"id": 999, "params": 1},
        ),
    )
    obj = store.objects[24]
    assert obj.czas == 500 and obj.url == "IAQ"
    assert _updated(applied) == [obj]
    assert 999 not in store.objects


def test_the_held_config_table_survives_an_eviction() -> None:
    """The catalogue carries no config column on this tier, so the held
    table re-applies on the re-creation after an eviction."""
    store = _app_store()
    _apply(store, PARAMS_DEVICES_TOPIC, params_table({"id": 24, "params": 65}))
    row = _app_row(24, "0_cb9b_74_0_1")
    _apply(store, DATA_DEVICES_TOPIC, details(row))
    _apply(store, DATA_DEVICES_TOPIC, details())  # the grant is revoked
    assert store.objects == {}
    _apply(store, DATA_DEVICES_TOPIC, details(row))  # and granted again
    assert store.objects[24].params == 65 and store.objects[24].read_only is True


def test_a_granted_object_the_config_table_misses_is_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The table covers the whole catalogue, so every granted object has a
    row. A gap leaves the object reading every config flag as unset, which
    is a server fault to name."""
    store = _app_store()
    _apply(store, DATA_DEVICES_TOPIC, details(_app_row(24, "0_cb9b_74_0_1")))
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        _apply(store, PARAMS_DEVICES_TOPIC, params_table({"id": 999, "params": 1}))
    assert store.missing_params_ids == frozenset({24})
    assert "[24]" in caplog.text
    # Warned once per change: the same gap on a refresh says nothing new.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._store"):
        _apply(store, PARAMS_DEVICES_TOPIC, params_table({"id": 999, "params": 1}))
    assert caplog.text == ""


def test_a_catalogue_row_with_no_config_row_yet_reports_no_gap() -> None:
    """A table still in flight is not a gap: the ids are unknown until it
    answers once."""
    store = _app_store()
    _apply(store, DATA_DEVICES_TOPIC, details(_app_row(24, "0_cb9b_74_0_1")))
    assert store.missing_params_ids == frozenset()


def test_details_row_czas_lands_raw_and_pulse_ms_reads_it_by_type() -> None:
    store = _store()
    _feed_catalogue(
        store,
        {"id": 41, "typ_komponentu": "przekaznik", "czas": 500},
        {"id": 42, "typ_komponentu": "roleta_procenty", "czas": 500},
    )
    assert store.objects[41].czas == 500
    assert store.objects[41].pulse_ms == 5000
    assert store.objects[42].czas == 500
    assert store.objects[42].pulse_ms == 0


def test_the_admin_store_reads_its_config_columns_from_the_params_table() -> None:
    """The object catalogue carries no Designer config column on either
    tier, so `data/params_devices` is every account's one source for
    `params`, `czas` and `url`."""
    store = _store()
    _feed_catalogue(store, {"id": 128, "params": 65, "czas": 500, "url": "IAQ"})
    obj = store.objects[128]
    assert obj.params == 65 and obj.czas == 500 and obj.url == "IAQ"
    assert store.missing_params_ids == frozenset()


def test_details_row_url_and_format_land_on_the_object() -> None:
    store = _store()
    _feed_catalogue(
        store, {"id": 128, "typ_komponentu": "bit32", "url": "", "format": "%.3f A"}
    )
    assert store.objects[128].url == ""
    assert store.objects[128].format == "%.3f A"


# --- cover tilt state ------------------------------------------------------


def test_lammel_is_parsed_into_the_object() -> None:
    store = _store()
    _feed_catalogue(
        store, {"id": 66, "typ_komponentu": "roleta_lamelki", "interpretacja": 1}
    )
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/66/state",
        '{ "state": "95","lammel": "65","block": "0" , "on": 1786723383804}',
    )
    obj = store.objects[66]
    assert obj.state == "95"
    assert obj.lammel == 65
    assert obj.supports_tilt is True
    assert isinstance(obj.kind, OutputKind)


def test_plain_cover_reports_no_tilt() -> None:
    store = _store()
    _feed_catalogue(
        store, {"id": 48, "typ_komponentu": "roleta_procenty", "interpretacja": 1}
    )
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/48/state",
        '{ "state": "55","block": "0","on": 1789000000000 }',
    )
    obj = store.objects[48]
    assert obj.state == "55"
    assert obj.lammel is None
    assert obj.supports_tilt is False


# --- cover block flag ------------------------------------------------------


def test_block_is_parsed_into_the_object() -> None:
    store = _store()
    _feed_catalogue(
        store, {"id": 48, "typ_komponentu": "roleta_procenty", "interpretacja": 1}
    )
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/48/state",
        '{ "state": "70","block": "1","on": 1789000000000 }',
    )
    obj = store.objects[48]
    assert obj.block == 1
    assert obj.blocks_closing is True
    assert obj.blocks_opening is False


def test_block_bits_read_per_direction() -> None:
    store = _store()
    _feed_catalogue(
        store, {"id": 48, "typ_komponentu": "roleta_procenty", "interpretacja": 1}
    )
    for value, closing, opening in (
        (0, False, False),
        (2, False, True),
        (3, True, True),
    ):
        _apply(
            store,
            f"ampio/fromDB/{USER}/ob/48/state",
            f'{{ "state": "70","block": "{value}","on": 1789000000000 }}',
        )
        obj = store.objects[48]
        assert obj.block == value
        assert obj.blocks_closing is closing
        assert obj.blocks_opening is opening


def test_object_without_block_reads_none() -> None:
    store = _store()
    _feed_catalogue(store, {"id": 12, "typ_komponentu": "przekaznik"})
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/12/state",
        '{ "state": "255","on": 1789000000000 }',
    )
    obj = store.objects[12]
    assert obj.block is None
    assert obj.blocks_closing is False
    assert obj.blocks_opening is False


def test_push_without_block_keeps_the_last_value() -> None:
    store = _store()
    _feed_catalogue(
        store, {"id": 48, "typ_komponentu": "roleta_procenty", "interpretacja": 1}
    )
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/48/state",
        '{ "state": "70","block": "3","on": 1789000000000 }',
    )
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/48/state",
        '{ "state": "80","on": 1789000000000 }',
    )
    assert store.objects[48].block == 3


def test_states_snapshot_seeds_block() -> None:
    store = _store()
    _feed_catalogue(
        store, {"id": 48, "typ_komponentu": "roleta_procenty", "opis_menu": "R"}
    )
    _apply(
        store,
        STATES_TOPIC,
        devices(
            {
                "id": 48,
                "stan_json": '{ "state": "100","block": "2" , "on": 1779560000000}',
            }
        ),
    )
    assert store.objects[48].block == 2


def test_states_snapshot_seeds_lammel() -> None:
    store = _store()
    _feed_catalogue(
        store, {"id": 66, "typ_komponentu": "roleta_lamelki", "opis_menu": "B"}
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
    assert store.objects[66].lammel == 100


# --- reg climate readback --------------------------------------------------

# A reg state as a live M-SERV serializes it: every field a string.
REG_PAYLOAD = (
    '{ "state": "0", "cooling": "0", "mode": "S",'
    '"measureTemp": "25.90","setTemperature": "21.00", "on": 1787682427583}'
)
REG_READBACK = ThermostatState(
    measure_temp=25.9,
    set_temperature=21.0,
    mode="S",
    cooling=False,
)


def test_reg_push_carries_thermostat_readback() -> None:
    store = _store()
    _feed_catalogue(store, {"id": 138, "typ_komponentu": "reg"})
    _apply(store, f"ampio/fromDB/{USER}/ob/138/state", REG_PAYLOAD)
    obj = store.objects[138]
    assert obj.state == "0"
    assert isinstance(obj.kind, ThermostatKind)
    assert obj.thermostat == REG_READBACK


def test_plain_push_keeps_last_readback() -> None:
    """A later report without the reg shape keeps the readback, like tilt."""
    store = _store()
    _feed_catalogue(store, {"id": 138, "typ_komponentu": "reg"})
    _apply(store, f"ampio/fromDB/{USER}/ob/138/state", REG_PAYLOAD)
    _apply(
        store,
        f"ampio/fromDB/{USER}/ob/138/state",
        '{"state": "1", "on": 1787682500000}',
    )
    obj = store.objects[138]
    assert obj.state == "1"
    assert obj.thermostat == REG_READBACK


def test_snapshot_readback_change_alone_dispatches() -> None:
    """A dated snapshot that moves only the readback still reports the
    object changed - a climate consumer must see the temperature tick."""
    store = _store()
    _feed_catalogue(store, {"id": 138, "typ_komponentu": "reg"})
    _apply(store, f"ampio/fromDB/{USER}/ob/138/state", REG_PAYLOAD)
    newer = REG_PAYLOAD.replace('"25.90"', '"26.40"').replace(
        "1787682427583", "1787682600000"
    )
    applied = _apply(store, STATES_TOPIC, devices({"id": 138, "stan_json": newer}))
    assert [o.id for o in _updated(applied)] == [138]
    assert store.objects[138].thermostat is not None
    assert store.objects[138].thermostat.measure_temp == 26.4


def test_states_snapshot_seeds_thermostat() -> None:
    store = _store()
    _feed_catalogue(store, {"id": 138, "typ_komponentu": "reg"})
    _apply(store, STATES_TOPIC, devices({"id": 138, "stan_json": REG_PAYLOAD}))
    assert store.objects[138].thermostat == REG_READBACK


def test_pending_reg_push_replays_thermostat() -> None:
    """A reg push racing ahead of its catalogue row keeps its readback."""
    store = _store()
    _apply(store, f"ampio/fromDB/{USER}/ob/138/state", REG_PAYLOAD)
    _feed_catalogue(store, {"id": 138, "typ_komponentu": "reg"})
    obj = store.objects[138]
    assert obj.state == "0"
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


def test_a_retained_diagnostics_replay_sets_the_values_but_not_last_seen() -> None:
    """The broker retains the last `b/4F` frame per module and replays it on
    every subscribe. The values land and `ModuleUpdated` fires, but the
    replay says nothing about whether the module is alive now."""
    store = _diag_store()

    applied = _apply(
        store,
        "ampio/from/CAFE/b/4F",
        '{"d":[254,79,63,142],"m":51966}',
        retained=True,
    )

    module = store.modules[7]
    assert module.supply_voltage == 12.6
    assert module.temperature == 42.0
    assert module.last_seen is None
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
    assert [(m.id, m.nazwa_urzadzenia) for m in _mod_updated(changed)] == [
        (2, "renamed")
    ]


def test_object_updated_carries_a_snapshot() -> None:
    """A dispatched event freezes the state it announced; later changes to
    the same object must not reach a listener that deferred processing.
    Objects are frozen, so the store publishes a new instance per change."""
    store = _store()
    _feed_catalogue(store, {"id": 5})
    state_topic = f"ampio/fromDB/{USER}/ob/5/state"
    (event,) = _updated(_apply(store, state_topic, '{"state": "1", "on": 2000}'))
    _apply(store, state_topic, '{"state": "2", "on": 3000}')
    assert event.state == "1"
    assert store.objects[5].state == "2"


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
    _feed_catalogue(store, _catalogue_row(id=9, opis_menu="Old name"))
    assert store.objects[9].name == "Old name"
    applied = _feed_catalogue(store, _catalogue_row(id=9, opis_menu=""))
    assert store.objects[9].name is None
    assert [o.id for o in _updated(applied)] == [9]


def test_updated_at_takes_the_report_date() -> None:
    """Every report stamps the M-SERV's own `on`, and 0 is a value rather
    than an absent stamp."""
    store = _store()
    _feed_catalogue(store, {"id": 9})
    topic = f"ampio/fromDB/{USER}/ob/9/state"
    _apply(store, topic, '{"state": "1", "on": 0}')
    assert store.objects[9].updated_at == 0.0
    _apply(store, topic, '{"state":"2","on":1789000000000}')
    assert store.objects[9].updated_at == 1789000000.0


def test_raw_owned_tracks_the_bridge_coverage() -> None:
    """Set by the first raw value the channel reports, replay included;
    cleared when the rebuilt index stops covering the object, so it goes
    back to per-object updates."""
    store = _panel_store()
    assert 50 not in store._raw_owned
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    assert 50 in store._raw_owned
    retyped = dict(_flaga_row(50, 32), typ_komponentu="roleta_procenty")
    _feed_catalogue(store, retyped)
    assert 50 not in store._raw_owned


# --- the retained replay lands before the catalogue -------------------------

# One module's health frame: 0.2 V steps and a 100 degree offset, so this
# reads 27.0 V and 23.0 degrees.
_DIAGNOSTICS = '{"d":[254,79,135,123],"m":51966}'


def test_a_retained_edge_before_the_catalogue_applies_at_the_fold() -> None:
    """The broker replays its retained channel values within a second of the
    subscribe, before any catalogue reply. Holding them is what makes the
    bridge live from the first connect instead of from the first press."""
    store = _store()
    _apply(store, "ampio/from/CAFE/state/f/32", "1", retained=True)
    assert store.objects == {}

    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    applied = _feed_catalogue(store, _flaga_row(50, 32))
    obj = store.objects[50]
    assert obj.state == "1" and 50 in store._raw_owned
    # The reply creates the object and the fold then fills it, so the last
    # event for it carries the replayed value.
    assert [o for o in _updated(applied) if o.id == 50][-1].state == "1"
    # A replay carries the value, not evidence that the module is alive.
    assert store.modules[7].last_seen is None


def test_a_retained_diagnostics_frame_before_the_module_list_applies_at_the_fold() -> (
    None
):
    store = _store()
    _apply(store, "ampio/from/CAFE/b/4F", _DIAGNOSTICS, retained=True)
    assert store.modules == {}

    applied = _apply(store, DEVICES_TOPIC, devices(_PANEL))
    module = store.modules[7]
    assert module.supply_voltage == 27.0 and module.temperature == 23.0
    assert module.last_seen is None
    assert [m for m in _mod_updated(applied) if m.id == 7][-1].supply_voltage == 27.0


def test_a_live_frame_for_an_unknown_channel_is_dropped() -> None:
    """Only a replay waits for the catalogue. A live frame for a channel no
    object exposes is one nothing will ever route."""
    store = _store()
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    _apply(store, "ampio/from/CAFE/b/4F", _DIAGNOSTICS)
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _flaga_row(50, 32))
    assert store.objects[50].state is None
    assert store.modules[7].supply_voltage is None


def test_the_held_replay_is_spent_once() -> None:
    """The fold clears what it applied, so a later catalogue reply does not
    re-apply a value the object has since moved past."""
    store = _store()
    _apply(store, "ampio/from/CAFE/state/f/32", "1", retained=True)
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _flaga_row(50, 32))
    _apply(store, "ampio/from/CAFE/state/f/32", "0")
    assert store.objects[50].state == "0"
    _feed_catalogue(store, _flaga_row(50, 32))
    assert store.objects[50].state == "0"


def test_a_held_frame_for_an_unlisted_module_waits_for_its_row() -> None:
    """The two catalogue replies arrive in no fixed order, so a held frame
    keeps waiting through a rebuild that does not list its module, and lands
    when one does."""
    store = _store()
    _apply(store, "ampio/from/BEEF/b/4F", _DIAGNOSTICS, retained=True)
    _apply(store, DEVICES_TOPIC, devices(_PANEL))  # mac CAFE, not BEEF
    assert store.modules[7].supply_voltage is None

    _apply(store, DEVICES_TOPIC, devices(_PANEL, {"id": 8, "mac": 0xBEEF}))
    assert store.modules[8].supply_voltage == 27.0


def test_a_held_channel_no_object_exposes_is_discarded() -> None:
    """Once the routing table exists, both catalogue replies have landed, so
    a held value nothing routes is stale weight rather than a value waiting
    for an object a later Designer save might add."""
    store = _store()
    _apply(store, "ampio/from/CAFE/state/f/99", "1", retained=True)
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _flaga_row(50, 32))
    assert store._pending_raw == {}

    # The object for that channel appears later and stays on its own path.
    _feed_catalogue(store, _flaga_row(50, 32), _flaga_row(52, 99))
    assert store.objects[52].state is None and 52 not in store._raw_owned


def test_a_channel_the_replay_skipped_keeps_the_per_object_path() -> None:
    """A channel the broker holds no frame for leaves its object unclaimed,
    so the per-object topic still feeds it. Nothing goes dark for want of a
    replay."""
    store = _store()
    _apply(store, "ampio/from/CAFE/state/f/32", "1", retained=True)
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _flaga_row(50, 32), _flaga_row(51, 33))
    assert 50 in store._raw_owned and store.objects[50].state == "1"
    assert 51 not in store._raw_owned

    _apply(store, f"ampio/fromDB/{USER}/ob/51/state", '{"state":"255","on":1700}')
    assert store.objects[51].state == "255"


def test_a_formerly_raw_owned_value_survives_a_skewed_snapshot() -> None:
    """Releasing the object hands it back to the per-object path, not to a
    skewed DB seed: the raw value stamped local time, so a dated snapshot
    waits for the next request cycle."""
    store = _panel_store()
    _apply(store, "ampio/from/CAFE/state/f/32", "1")
    retyped = dict(_flaga_row(50, 32), typ_komponentu="roleta_procenty")
    _feed_catalogue(store, retyped)
    assert 50 not in store._raw_owned
    far_future = int((time.time() + 3600) * 1000)
    stan = json.dumps({"state": "0", "on": far_future})
    _apply(store, STATES_TOPIC, json.dumps({"List": [{"id": 50, "stan_json": stan}]}))
    assert store.objects[50].state == "1"


def _collisions(applied: Applied) -> list[NotConfigured]:
    return [e for e in applied.events if isinstance(e, NotConfigured)]


def test_two_module_rows_on_one_mac_are_refused_at_the_door() -> None:
    store = _store()
    applied = _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB, 0xB))
    assert list(store.modules) == [1]
    assert store.collisions == ((0xB, (2, 3)),)
    assert [e.collisions for e in _collisions(applied)] == [((0xB, (2, 3)),)]
    assert store.admission_failure().collisions == ((0xB, (2, 3)),)
    assert store.module_by_mac(0xB) is None


def test_a_collision_found_after_connect_evicts_both_rows() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB, 0xC))
    applied = _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB, 0xB))
    assert [type(e) for e in applied.events] == [
        ModuleRemoved,
        ModuleRemoved,
        NotConfigured,
    ]
    assert sorted(
        e.module.id for e in applied.events if isinstance(e, ModuleRemoved)
    ) == [
        2,
        3,
    ]
    assert list(store.modules) == [1]
    again = _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB, 0xB))
    assert _collisions(again) == []


def test_a_reordered_collision_reply_emits_nothing_new() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_module_row(2, 0xB), _module_row(3, 0xB)))
    again = _apply(
        store, DEVICES_TOPIC, devices(_module_row(3, 0xB), _module_row(2, 0xB))
    )
    assert store.collisions == ((0xB, (2, 3)),)
    assert _collisions(again) == []


def test_a_resolved_collision_re_admits_the_rows() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB, 0xB))
    applied = _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB, 0xC))
    assert sorted(store.modules) == [1, 2, 3]
    assert store.collisions == ()
    assert _collisions(applied) == []
    assert store.admission_failure() is None


def test_admission_failure_carries_both_installer_faults() -> None:
    """One store can hold a leafless object row and a shared module mac,
    and the failure names each fault in its own sentence."""
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB, 0xB))
    _feed_catalogue(store, {**_flaga_row(41, 3, mac=0xA), "leafId": ""})
    failure = store.admission_failure()
    assert failure.objects == ((41, "Flag"),)
    assert failure.collisions == ((0xB, (2, 3)),)
    assert "41 (Flag) carry no leaf" in str(failure)
    assert "modules 2, 3 share the override mac b" in str(failure)


# --- the sweep datasets ------------------------------------------------------


def _sweep(store: AdminStore, answered: set[int], **datasets: dict) -> None:
    """One sweep pass: the macs that answered and what they carried."""
    store.apply_sweep(
        frozenset(answered),
        datasets.get("records", {}),
        datasets.get("cover_parameters", {}),
        datasets.get("module_records", {}),
        datasets.get("capabilities", {}),
        datasets.get("panel_settings", {}),
    )


def test_a_sweep_holds_the_datasets_by_id_and_by_mac() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA))
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA))
    _sweep(
        store,
        {0xA},
        records={41: DesignerRecord(location="Hall")},
        cover_parameters={41: _COVER_PARAMS},
        module_records={0xA: ModuleRecord(desc="Box")},
        capabilities={0xA: {5: 4}},
    )
    assert store.records[41] == DesignerRecord(location="Hall")
    assert store.cover_parameters[41] == _COVER_PARAMS
    assert store.module_records[0xA] == ModuleRecord(desc="Box")
    assert store.capabilities[0xA] == {5: 4}


def test_a_sweep_replaces_every_entry_of_an_answered_mac() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA, 0xB))
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA), _flaga_row(42, 3, mac=0xB))
    _sweep(
        store,
        {0xA, 0xB},
        records={
            41: DesignerRecord(location="Hall"),
            42: DesignerRecord(location="Bath"),
        },
        cover_parameters={41: _COVER_PARAMS, 42: _COVER_PARAMS},
        capabilities={0xA: {5: 4}, 0xB: {}},
        panel_settings={0xA: _PANEL_SETTINGS, 0xB: _PANEL_SETTINGS},
    )
    _sweep(store, {0xA}, records={}, capabilities={0xA: {}})
    assert 41 not in store.records
    assert 41 not in store.cover_parameters
    assert store.records[42] == DesignerRecord(location="Bath")
    assert store.cover_parameters[42] == _COVER_PARAMS
    assert store.capabilities == {0xA: {}, 0xB: {}}
    assert list(store.panel_settings) == [0xB]


def test_a_sweep_emits_nothing() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA))
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA))
    assert (
        store.apply_sweep(
            frozenset({0xA}), {41: DesignerRecord()}, {}, {}, {0xA: {}}, {}
        )
        is None
    )


def test_an_eviction_drops_the_object_datasets() -> None:
    store = _store()
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA))
    _sweep(
        store,
        {0xA},
        records={41: DesignerRecord(location="Hall")},
        cover_parameters={41: _COVER_PARAMS},
    )
    _feed_catalogue(store)
    assert 41 not in store.records
    assert 41 not in store.cover_parameters


def test_a_moved_leaf_drops_the_object_datasets() -> None:
    store = _store()
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA))
    _sweep(
        store,
        {0xA},
        records={41: DesignerRecord(location="Hall")},
        cover_parameters={41: _COVER_PARAMS},
    )
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xB))
    assert 41 not in store.records
    assert 41 not in store.cover_parameters


def test_a_module_eviction_drops_its_mac_datasets() -> None:
    """The mac-keyed entries belong to the module row that left, so they
    leave with it."""
    store = _store()
    _apply(store, DEVICES_TOPIC, _devices(0xA))
    _feed_catalogue(store, _flaga_row(41, 3, mac=0xA))
    _sweep(
        store,
        {0xA},
        module_records={0xA: ModuleRecord(desc="Box")},
        capabilities={0xA: {5: 4}},
        panel_settings={0xA: _PANEL_SETTINGS},
    )
    _apply(store, DEVICES_TOPIC, devices())
    assert 0xA not in store.module_records
    assert 0xA not in store.capabilities
    assert 0xA not in store.panel_settings


def test_a_kind_change_away_from_a_cover_drops_the_parameters() -> None:
    """Travel parameters belong to a roller channel, so an object that
    leaves the roller class keeps none, and a change back reads nothing
    until a sweep answers again."""
    store = _store()
    cover = {**_flaga_row(41, 3, mac=0xA), "typ_komponentu": "roleta_procenty"}
    _feed_catalogue(store, cover)
    _sweep(store, {0xA}, cover_parameters={41: _COVER_PARAMS})
    _feed_catalogue(store, {**cover, "typ_komponentu": "przekaznik"})
    assert 41 not in store.cover_parameters
    _feed_catalogue(store, cover)
    assert 41 not in store.cover_parameters


def test_the_base_store_holds_no_dataset() -> None:
    store = _app_store()
    _feed_catalogue(store, {"id": 41})
    assert not hasattr(store, "records")
    _feed_catalogue(store)
    assert 41 not in store.objects


def test_a_sweep_never_touches_the_catalogue_type_column() -> None:
    store = _store()
    _feed_catalogue(store, {**_flaga_row(41, 3, mac=0xA), "type": "256"})
    _sweep(store, {0xA}, records={41: DesignerRecord(matter_device_type=515)})
    assert store.objects[41].matter_device_type == 256


# --- ObjectAdded: an object's first event -----------------------------------


def test_new_catalogue_row_dispatches_object_added() -> None:
    store = AdminStore()
    applied = _feed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"})
    assert [type(e) for e in applied.events] == [ObjectAdded]
    assert applied.events[0].object.id == 7
    # The same reply again says nothing new.
    assert _feed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"}).events == []


def test_known_row_change_dispatches_updated_not_added() -> None:
    store = AdminStore()
    _feed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"})
    applied = _feed_catalogue(
        store, {"id": 7, "typ_komponentu": "flaga", "opis_menu": "x"}
    )
    assert [type(e) for e in applied.events] == [ObjectUpdated]


def test_recreation_after_eviction_dispatches_added_again() -> None:
    store = AdminStore()
    _feed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"})
    removed = _feed_catalogue(store)  # empty catalogue evicts
    assert [type(e) for e in removed.events] == [ObjectRemoved]
    readded = _feed_catalogue(store, {"id": 7, "typ_komponentu": "flaga"})
    assert [type(e) for e in readded.events] == [ObjectAdded]


def test_bare_row_creation_still_dispatches_added() -> None:
    store = AdminStore()
    applied = _feed_catalogue(store, {"id": 9})
    assert [type(e) for e in applied.events] == [ObjectAdded]


# --- raw-channel output bridge (classic panel status LEDs) -----------------


_RELAY_MODULE = {"id": 8, "mac": 0xB0B0, "typ_urzadzenia": 4, "nazwa_urzadzenia": "r"}


def _przekaznik_row(oid: int, funkcja: int, leaf: str) -> dict:
    return {
        "id": oid,
        "typ_komponentu": "przekaznik",
        "interpretacja": funkcja,
        "funkcja": funkcja,
        "leafId": leaf,
        "opis_menu": "Out",
    }


def test_panel_output_o_channel_routes_to_its_object() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _przekaznik_row(90, 2, "0_cafe_257_2_1"))

    applied = _apply(store, "ampio/from/CAFE/state/o/2", "1")

    obj = store.objects[90]
    assert obj.state == "1" and obj.is_on is True and 90 in store._raw_owned
    assert _updated(applied) == [obj]


def test_o_channel_of_a_relay_module_is_bridged_too() -> None:
    """The o bridge covers every przekaznik uniformly - a relay's outputs
    share the channel shape, so they gain the same raw-first path."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_RELAY_MODULE))
    _feed_catalogue(store, _przekaznik_row(91, 1, "0_b0b0_257_2_0"))

    applied = _apply(store, "ampio/from/B0B0/state/o/1", "1")

    obj = store.objects[91]
    assert obj.state == "1" and 91 in store._raw_owned
    assert _updated(applied) == [obj]


_INOC_MODULE = {"id": 9, "mac": 0x1A2B, "typ_urzadzenia": 14, "nazwa_urzadzenia": "oc"}


def test_a_channel_routes_to_an_open_collector_relay() -> None:
    """A przekaznik on leaf class 67 reports on the `a` prefix, not `o`."""
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_INOC_MODULE))
    _feed_catalogue(store, _przekaznik_row(93, 8, "0_1a2b_67_0_7"))

    ignored = _apply(store, "ampio/from/1A2B/state/o/8", "1")
    assert _updated(ignored) == []

    applied = _apply(store, "ampio/from/1A2B/state/a/8", "255")
    obj = store.objects[93]
    assert obj.state == "255" and obj.is_on is True and 93 in store._raw_owned
    assert _updated(applied) == [obj]


def test_a_channel_does_not_route_a_binary_output_relay() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_RELAY_MODULE))
    _feed_catalogue(store, _przekaznik_row(91, 1, "0_b0b0_257_2_0"))

    applied = _apply(store, "ampio/from/B0B0/state/a/1", "255")
    assert _updated(applied) == []
    assert 91 not in store._raw_owned


def test_panel_output_per_object_echo_is_dropped_once_raw_owned() -> None:
    store = _store()
    _apply(store, DEVICES_TOPIC, devices(_PANEL))
    _feed_catalogue(store, _przekaznik_row(90, 2, "0_cafe_257_2_1"))
    _apply(store, "ampio/from/CAFE/state/o/2", "1")

    applied = _apply(
        store, f"ampio/fromDB/{USER}/ob/90/state", '{"state":"255","on":1789000000000}'
    )

    assert store.objects[90].state == "1"
    assert _updated(applied) == []
