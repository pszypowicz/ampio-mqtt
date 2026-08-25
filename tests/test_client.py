"""Tests for the AmpioClient surface: listener wiring, lifecycle entry
points, diagnostics retention, and stats. The store's message semantics are
covered in test_store.py; the pure model properties in test_models.py."""

from __future__ import annotations

import asyncio
import dataclasses

import aiomqtt
import pytest
from conftest import (
    ADMIN_USER,
    DATA_DEVICES_TOPIC,
    DETAILS_TOPIC,
    DEVICES_TOPIC,
    INFO_TOPIC,
    PARAMS_DEVICES_TOPIC,
    STATES_TOPIC,
    USER,
    FakeBroker,
    details,
    devices,
    feed,
    info,
)

from ampio_mqtt import (
    AccessTier,
    AmpioClient,
    AmpioConnectionError,
    AvailabilityChanged,
    ModuleRemoved,
    ObjectRemoved,
    ObjectUpdated,
)


def _flaga(oid: int, funkcja: int, dev: int = 7) -> dict:
    return {
        "id": oid,
        "id_urzadzenia": dev,
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "funkcja": funkcja,
        "opis_menu": "Flag",
    }


def _client() -> AmpioClient:
    return AmpioClient("host", username=USER)


def _admin_client() -> AmpioClient:
    """For the module-catalogue machinery, which only the admin tier is served."""
    return AmpioClient("host", username=ADMIN_USER)


def test_mserv_prefers_info_mac_cross_check() -> None:
    """mserv is the module row whose mac_global matches info.mac."""
    client = _admin_client()
    # Two modules with typ != 10 plus the actual M-SERV (typ=10, mac_global=47846).
    feed(
        client,
        f"ampio/fromDB/{ADMIN_USER}/config/devices",
        devices(
            {
                "id": 1,
                "mac": 1,
                "mac_global": 47846,
                "typ_urzadzenia": 10,
                "nazwa_urzadzenia": "MSERV",
            },
            {
                "id": 2,
                "mac": 2,
                "mac_global": 1000,
                "typ_urzadzenia": 4,
                "nazwa_urzadzenia": "MREL",
            },
        ),
    )
    feed(
        client,
        f"ampio/fromDB/{ADMIN_USER}/data/info",
        info(serverVersion="1", mac="47846"),
    )
    mserv = client.mserv
    assert mserv is not None and mserv.id == 1 and mserv.name == "MSERV"


# Type 10 is the M-SERV-s; type 0 is VIRTUAL. Both are hub types.
@pytest.mark.parametrize("hub_type", [10, 0])
def test_mserv_falls_back_to_unique_hub_module(hub_type: int) -> None:
    """Without info, the unique hub-typed module identifies the M-SERV,
    with a VIRTUAL hub resolving exactly like an M-SERV one."""
    client = _admin_client()
    feed(
        client,
        f"ampio/fromDB/{ADMIN_USER}/config/devices",
        devices(
            {
                "id": 5,
                "mac": 1,
                "mac_global": 12345,
                "typ_urzadzenia": hub_type,
                "nazwa_urzadzenia": "HUB",
            },
            {
                "id": 6,
                "mac": 2,
                "mac_global": 67890,
                "typ_urzadzenia": 4,
                "nazwa_urzadzenia": "MREL",
            },
        ),
    )
    mserv = client.mserv
    assert mserv is not None and mserv.id == 5


# --- module_for: the mac-validated object-to-module join (#93) --------------


ADMIN_DEVICES = f"ampio/fromDB/{ADMIN_USER}/config/devices"
ADMIN_DETAILS = f"ampio/fromDB/{ADMIN_USER}/config/devicesDetails"


def _module_row(mid: int, mac: int | None, name: str = "MREL") -> dict:
    row: dict = {
        "id": mid,
        "mac_global": 1000 + mid,
        "typ_urzadzenia": 4,
        "nazwa_urzadzenia": name,
    }
    if mac is not None:
        row["mac"] = mac
    return row


def _object_row(oid: int, dev: int | None, mac_hex: str | None) -> dict:
    row: dict = {"id": oid, "typ_komponentu": "flaga", "opis_menu": "Flag"}
    if dev is not None:
        row["id_urzadzenia"] = dev
    if mac_hex is not None:
        row["leafId"] = f"0_{mac_hex}_1_0_0"
    return row


def test_module_for_returns_the_mac_agreeing_row() -> None:
    client = _admin_client()
    feed(client, ADMIN_DEVICES, devices(_module_row(7, 0xCAFE)))
    feed(client, ADMIN_DETAILS, details(_object_row(10, 7, "cafe")))
    module = client.module_for(client.objects[10])
    assert module is not None
    assert module.id == 7


def test_module_for_rejects_a_mac_disagreement() -> None:
    """device_id pointing at a row whose mac is not the object's leaf mac
    is the stale-join shape a module replacement produces; None beats the
    wrong module."""
    client = _admin_client()
    feed(client, ADMIN_DEVICES, devices(_module_row(7, 0xCAFE)))
    feed(client, ADMIN_DETAILS, details(_object_row(10, 7, "beef")))
    assert client.module_for(client.objects[10]) is None


@pytest.mark.parametrize(
    ("module_mac", "leaf_hex"),
    [(0xCAFE, None), (None, "cafe"), (None, None)],
)
def test_module_for_requires_a_proven_agreement(
    module_mac: int | None, leaf_hex: str | None
) -> None:
    """A missing mac on either side is an unvalidated join, not a match -
    two Nones agreeing proves nothing."""
    client = _admin_client()
    feed(client, ADMIN_DEVICES, devices(_module_row(7, module_mac)))
    feed(client, ADMIN_DETAILS, details(_object_row(10, 7, leaf_hex)))
    assert client.module_for(client.objects[10]) is None


def test_module_for_without_a_join_key() -> None:
    client = _admin_client()
    feed(client, ADMIN_DEVICES, devices(_module_row(7, 0xCAFE)))
    feed(
        client,
        ADMIN_DETAILS,
        details(_object_row(10, None, "cafe"), _object_row(11, 99, "cafe")),
    )
    # No device_id, and a device_id no row answers.
    assert client.module_for(client.objects[10]) is None
    assert client.module_for(client.objects[11]) is None


def test_module_for_resolves_colliding_macs_by_the_join() -> None:
    """Override macs may collide across rows; the join picks the row, the
    mac only gates it."""
    client = _admin_client()
    feed(
        client,
        ADMIN_DEVICES,
        devices(_module_row(7, 0xCAFE, "FIRST"), _module_row(8, 0xCAFE, "SECOND")),
    )
    feed(client, ADMIN_DETAILS, details(_object_row(10, 8, "cafe")))
    module = client.module_for(client.objects[10])
    assert module is not None
    assert (module.id, module.name) == (8, "SECOND")


def test_module_for_is_none_on_the_restricted_tier() -> None:
    """The restricted tier never receives the module catalogue, so the
    resolver is honest about having no row to validate."""
    client = _client()
    feed(client, DATA_DEVICES_TOPIC, details(_object_row(10, 7, "cafe")))
    assert client.objects[10].module_mac == 0xCAFE
    assert client.module_for(client.objects[10]) is None


def test_mserv_matches_the_override_mac_arm() -> None:
    """The cross-check accepts the Designer override mac as well as the
    factory id: after a hardware swap mac_global changes but the
    re-stamped override does not."""
    client = _admin_client()
    feed(
        client,
        f"ampio/fromDB/{ADMIN_USER}/config/devices",
        devices(
            {
                "id": 4,
                "mac": 47846,
                "mac_global": 999,
                "typ_urzadzenia": 4,
                "nazwa_urzadzenia": "SWAPPED",
            }
        ),
    )
    feed(client, f"ampio/fromDB/{ADMIN_USER}/data/info", info(mac="47846"))
    mserv = client.mserv
    assert mserv is not None and mserv.id == 4


def test_read_surface_is_immutable() -> None:
    """Neither the mappings nor the frozen instances in them can be mutated
    from consumer code - the promise core builds its entity layer on."""
    client = _admin_client()
    feed(
        client,
        f"ampio/fromDB/{ADMIN_USER}/config/devicesDetails",
        details(_flaga(41, 3)),
    )
    feed(
        client,
        f"ampio/fromDB/{ADMIN_USER}/config/devices",
        devices({"id": 7, "mac": 1}),
    )
    with pytest.raises(TypeError):
        client.objects[99] = client.objects[41]  # type: ignore[index]
    with pytest.raises(TypeError):
        del client.objects[41]  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        client.objects[41].name = "TAMPERED"  # type: ignore[misc]
    with pytest.raises(TypeError):
        client.modules[99] = client.modules[7]  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        client.modules[7].name = "TAMPERED"  # type: ignore[misc]


def test_mserv_none_when_ambiguous_and_no_info() -> None:
    """If multiple modules are typ=10 and no info reply, do not guess."""
    client = _admin_client()
    feed(
        client,
        f"ampio/fromDB/{ADMIN_USER}/config/devices",
        devices(
            {
                "id": 1,
                "mac": 1,
                "mac_global": 1,
                "typ_urzadzenia": 10,
                "nazwa_urzadzenia": "MSERV-A",
            },
            {
                "id": 2,
                "mac": 2,
                "mac_global": 2,
                "typ_urzadzenia": 10,
                "nazwa_urzadzenia": "MSERV-B",
            },
        ),
    )
    assert client.mserv is None


def test_state_updates_object_and_notifies() -> None:
    client = _client()
    feed(
        client,
        DATA_DEVICES_TOPIC,
        details(
            {
                "id": 41,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "Salon",
            }
        ),
    )
    received: list = []
    client.subscribe(lambda e: received.append(e.object), of=ObjectUpdated)

    feed(
        client,
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{ "state": "22.5","desc": "22.5 C" , "on": 1779555459594} ',
    )
    obj = client.objects[41]
    assert obj.value == "22.5"
    assert received == [obj]


def test_object_removal_listener_fires_after_eviction() -> None:
    client = _client()
    removed: list[int] = []
    unsubscribe = client.subscribe(
        lambda e: removed.append(e.object.id), of=ObjectRemoved
    )
    topic = DATA_DEVICES_TOPIC
    feed(client, topic, details(_flaga(41, 3), _flaga(42, 4)))
    feed(client, topic, details(_flaga(41, 3)))
    assert removed == [42]
    assert 42 not in client.objects

    unsubscribe()
    feed(client, topic, details(_flaga(41, 3), _flaga(42, 4)))
    feed(client, topic, details(_flaga(41, 3)))
    assert removed == [42]


def test_unsubscribe_is_idempotent() -> None:
    """Consumer teardown lists routinely invoke a cleanup callback twice;
    the second call must be a no-op, not a ValueError."""
    client = _client()
    unsubscribe = client.subscribe(lambda e: None, of=ObjectRemoved)
    unsubscribe()
    unsubscribe()


def test_unsubscribe_removes_only_its_own_registration() -> None:
    """The same listener registered twice is dispatched twice; either
    unsubscribe drops exactly its own registration, however often called."""
    client = _client()
    seen: list[int] = []

    def listener(e: ObjectUpdated) -> None:
        seen.append(e.object.id)

    first = client.subscribe(listener, of=ObjectUpdated)
    client.subscribe(listener, of=ObjectUpdated)
    topic = DATA_DEVICES_TOPIC
    feed(client, topic, details(_flaga(41, 3)))
    assert seen == [41, 41]

    first()
    first()  # repeat must not touch the surviving registration
    feed(client, topic, details(_flaga(41, 4)))
    assert seen == [41, 41, 41]


def test_module_removal_dispatches_module_removed() -> None:
    client = _admin_client()
    removed: list[int] = []
    client.subscribe(lambda e: removed.append(e.module.id), of=ModuleRemoved)
    topic = f"ampio/fromDB/{ADMIN_USER}/config/devices"
    feed(client, topic, devices({"id": 1, "mac": 1}, {"id": 2, "mac": 2}))
    feed(client, topic, devices({"id": 1, "mac": 1}))
    assert removed == [2]
    assert 2 not in client.modules


def test_subscribe_filters_and_preserves_order() -> None:
    """One stream, processing order; `of` narrows to the named classes."""
    client = _client()
    everything: list[object] = []
    only_updates: list[object] = []
    client.subscribe(everything.append)
    client.subscribe(only_updates.append, of=ObjectUpdated)
    topic = DATA_DEVICES_TOPIC
    feed(client, topic, details(_flaga(41, 3), _flaga(42, 4)))
    feed(client, topic, details(_flaga(41, 3)))
    assert [type(e).__name__ for e in everything] == [
        "ObjectUpdated",
        "ObjectUpdated",
        "ObjectRemoved",
    ]
    assert [type(e).__name__ for e in only_updates] == [
        "ObjectUpdated",
        "ObjectUpdated",
    ]


def test_subscribe_object_id_dispatches_only_matching_object() -> None:
    """An object_id-filtered listener runs for its own object's events
    and never enters for any other object's (#99)."""
    client = _client()
    mine: list[int] = []
    other: list[int] = []
    client.subscribe(lambda e: mine.append(e.object.id), of=ObjectUpdated, object_id=41)
    client.subscribe(
        lambda e: other.append(e.object.id), of=ObjectUpdated, object_id=42
    )
    feed(client, DATA_DEVICES_TOPIC, details(_flaga(41, 3), _flaga(42, 4)))
    assert mine == [41]
    assert other == [42]


def test_subscribe_object_id_tuple_covers_update_and_removal() -> None:
    """One registration with of=(ObjectUpdated, ObjectRemoved) follows a
    single object across update and removal, in production order."""
    client = _client()
    seen: list[str] = []
    client.subscribe(
        lambda e: seen.append(type(e).__name__),
        of=(ObjectUpdated, ObjectRemoved),
        object_id=42,
    )
    feed(client, DATA_DEVICES_TOPIC, details(_flaga(41, 3), _flaga(42, 4)))
    feed(client, DATA_DEVICES_TOPIC, details(_flaga(41, 3)))
    assert seen == ["ObjectUpdated", "ObjectRemoved"]


def test_subscribe_object_id_requires_object_bearing_classes() -> None:
    """object_id is meaningful only for events that carry .object; any
    other combination - a missing of included - is a registration-time
    ValueError, so a listener that can never fire is never registered."""
    client = _client()
    with pytest.raises(ValueError):
        client.subscribe(
            lambda e: None,
            of=AvailabilityChanged,  # type: ignore[arg-type]
            object_id=1,
        )
    with pytest.raises(ValueError):
        client.subscribe(
            lambda e: None,
            of=(ObjectUpdated, ModuleRemoved),  # type: ignore[arg-type]
            object_id=1,
        )
    with pytest.raises(ValueError):
        client.subscribe(lambda e: None, object_id=1)  # type: ignore[call-overload]


def test_subscribe_object_id_unsubscribe_contract() -> None:
    """The same listener registered twice on one object id is dispatched
    twice; either unsubscribe drops exactly its own registration and is
    idempotent, like the class-only path."""
    client = _client()
    seen: list[int] = []

    def listener(e: ObjectUpdated) -> None:
        seen.append(e.object.id)

    first = client.subscribe(listener, of=ObjectUpdated, object_id=41)
    client.subscribe(listener, of=ObjectUpdated, object_id=41)
    feed(client, DATA_DEVICES_TOPIC, details(_flaga(41, 3)))
    assert seen == [41, 41]

    first()
    first()  # repeat must not touch the surviving registration
    feed(client, DATA_DEVICES_TOPIC, details(_flaga(41, 4)))
    assert seen == [41, 41, 41]


def test_subscribe_object_id_last_unsubscribe_drops_the_bucket() -> None:
    """Entity churn must not grow the per-object index without bound: the
    last unsubscribe for an id removes its bucket entirely."""
    client = _client()
    unsub_a = client.subscribe(lambda e: None, of=ObjectUpdated, object_id=7)
    unsub_b = client.subscribe(lambda e: None, of=ObjectUpdated, object_id=7)
    unsub_a()
    assert 7 in client._by_object
    unsub_b()
    assert 7 not in client._by_object


def test_subscribe_object_id_listener_can_unsubscribe_mid_dispatch() -> None:
    """A listener that unsubscribes itself while its own event is being
    dispatched neither breaks the walk nor silences its neighbours."""
    client = _client()
    calls: list[str] = []

    def one_shot(e: ObjectUpdated) -> None:
        calls.append("one_shot")
        unsub()

    unsub = client.subscribe(one_shot, of=ObjectUpdated, object_id=41)
    client.subscribe(lambda e: calls.append("steady"), of=ObjectUpdated, object_id=41)
    feed(client, DATA_DEVICES_TOPIC, details(_flaga(41, 3)))
    feed(client, DATA_DEVICES_TOPIC, details(_flaga(41, 4)))
    assert calls == ["one_shot", "steady", "steady"]


def test_subscribe_object_id_listener_exception_is_isolated() -> None:
    """A raising ID-filtered listener is logged and the remaining
    listeners for the same object still run, like the class-only path."""
    client = _client()
    seen: list[int] = []

    def broken(e: ObjectUpdated) -> None:
        raise RuntimeError("listener bug")

    client.subscribe(broken, of=ObjectUpdated, object_id=41)
    client.subscribe(lambda e: seen.append(e.object.id), of=ObjectUpdated, object_id=41)
    feed(client, DATA_DEVICES_TOPIC, details(_flaga(41, 3)))
    assert seen == [41]


async def test_start_times_out_without_auth_error() -> None:
    """A connection that simply never comes up still raises AmpioConnectionError."""
    broker = FakeBroker()
    broker.enter_delay = 10.0
    client = AmpioClient(
        "host",
        username=USER,
        password="p",
        reconnect_interval=0.0,
        mqtt_client_factory=broker.factory,
    )
    with pytest.raises(AmpioConnectionError):
        await client.start(timeout=0.1, discovery_timeout=0.05)


def test_last_payloads_retained_for_each_handler() -> None:
    """Each discovery handler stashes the verbatim payload for diagnostics,
    keyed by endpoint name and scoped to the endpoints the tier is served."""
    admin = AmpioClient("host", username="admin")
    devices_payload = devices({"id": 1, "mac": 1, "typ_urzadzenia": 10})
    details_payload = details(
        {"id": 5, "id_urzadzenia": 1, "typ_komponentu": "temp", "interpretacja": 1}
    )
    feed(admin, "ampio/fromDB/admin/config/devices", devices_payload)
    feed(admin, "ampio/fromDB/admin/config/devicesDetails", details_payload)
    assert admin.last_payloads["devices"] == devices_payload
    assert admin.last_payloads["details"] == details_payload

    client = _client()
    info_payload = info(mac=12345, serverVersion="2025")
    states_payload = devices({"id": 5, "stan_json": '{"state":"1"}'})
    data_devices_payload = details({"id": 5, "typ_komponentu": "temp"})
    params_payload = devices({"id": 5, "params": 17})
    scenes_payload = devices({"id": 3, "sceneName": "Evening"})
    groups_payload = devices({"id": 1, "opis_menu": "Salon"})
    group_devices_payload = devices({"id_grupy": 1, "id_obiektu": 5})
    feed(client, f"ampio/fromDB/{USER}/data/info", info_payload)
    feed(client, f"ampio/fromDB/{USER}/data/states", states_payload)
    feed(client, f"ampio/fromDB/{USER}/data/devices", data_devices_payload)
    feed(client, f"ampio/fromDB/{USER}/data/params_devices", params_payload)
    feed(client, f"ampio/fromDB/{USER}/data/scenes", scenes_payload)
    feed(client, f"ampio/fromDB/{USER}/data/groups", groups_payload)
    feed(client, f"ampio/fromDB/{USER}/data/group_devices", group_devices_payload)
    assert client.last_payloads["info"] == info_payload
    assert client.last_payloads["states"] == states_payload
    assert client.last_payloads["data_devices"] == data_devices_payload
    assert client.last_payloads["params_devices"] == params_payload
    assert client.last_payloads["scenes"] == scenes_payload
    assert client.last_payloads["groups"] == groups_payload
    assert client.last_payloads["group_devices"] == group_devices_payload

    # An admin-surface topic on a restricted client is unroutable: the
    # tier is not served that surface, so nothing is retained for it.
    feed(client, f"ampio/fromDB/{USER}/config/devices", devices_payload)
    assert "devices" not in client.last_payloads
    assert client.modules == {}


def test_access_tier_is_the_authenticated_username() -> None:
    """The broker authenticates the login at CONNACK and only the reserved
    `admin` name is the administrator, so the tier is a constructor fact."""
    assert _client().access_tier is AccessTier.RESTRICTED
    assert AmpioClient("host", username="admin").access_tier is AccessTier.ADMIN


def test_dispatch_updates_last_message_at() -> None:
    """Every dispatched MQTT message advances the connection stats clock."""
    client = _client()
    assert client.stats.last_message_at is None
    feed(client, f"ampio/fromDB/{USER}/data/info", info(mac=1))
    assert client.stats.last_message_at is not None
    first = client.stats.last_message_at
    feed(client, f"ampio/fromDB/{USER}/data/info", info(mac=1))
    assert client.stats.last_message_at >= first


async def test_reconnect_count_increments_on_reconnect() -> None:
    """A poison-then-recover cycle bumps reconnect_count and captures the error."""
    broker = FakeBroker()
    # First connection: the stream drops so the runner reconnects. The
    # availability listener below clears the poison synchronously inside that
    # drop, so the second connection stays up and the count lands at exactly 1.
    broker.stream_error = aiomqtt.MqttError("simulated drop")
    client = AmpioClient(
        "host",
        username=USER,
        password="p",
        reconnect_interval=0.0,
        mqtt_client_factory=broker.factory,
    )

    def _recover(event: AvailabilityChanged) -> None:
        if not event.available:
            broker.stream_error = None

    client.subscribe(_recover, of=AvailabilityChanged)
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        # Wait for the second connection to land (reconnect_count incremented).
        async with asyncio.timeout(2.0):
            while client.stats.reconnect_count < 1:
                await asyncio.sleep(0.01)
    finally:
        await client.stop()

    assert client.stats.reconnect_count == 1
    assert client.stats.started_at is not None
    assert client.stats.last_error == "simulated drop"


async def test_discovery_stays_incomplete_without_server_identity(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """An info reply without a mac must not complete discovery: a True
    wait promises the identity a consumer scopes its registry by (#78)."""
    client, _broker = connected
    feed(client, STATES_TOPIC, devices())
    feed(client, INFO_TOPIC, info())  # unparseable: carries no identity
    feed(client, DATA_DEVICES_TOPIC, details())
    feed(client, PARAMS_DEVICES_TOPIC, devices())
    assert await client.wait_for_initial_discovery(timeout=0.05) is False
    assert client.server_info is None

    feed(client, INFO_TOPIC, info(mac=555, userId=-1, serverVersion="1865"))
    feed(client, DETAILS_TOPIC, details())
    feed(client, DEVICES_TOPIC, devices())
    assert await client.wait_for_initial_discovery(timeout=1.0) is True
    assert client.server_info.key == "555"


@pytest.mark.parametrize("username", [None, ""])
async def test_username_is_required(username: str | None) -> None:
    """Every topic is namespaced by account; without one the client would
    subscribe to `ampio/fromDB//...` - a namespace no M-SERV serves - and
    fail minutes later as discovery that never completes."""
    with pytest.raises(ValueError):
        AmpioClient("host", username=username)
    with pytest.raises(ValueError):
        await AmpioClient.test_connection("host", username, None)
