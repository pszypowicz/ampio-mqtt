"""Tests for the AmpioClient surface: listener wiring, lifecycle entry
points, diagnostics retention, stats, and the model properties consumers
read. The store's message semantics are covered in test_store.py."""

from __future__ import annotations

import asyncio
import json

import aiomqtt
import pytest
from conftest import USER, FakeBroker, details, devices, feed, info
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from ampio_mqtt import (
    AccessTier,
    AmpioAuthError,
    AmpioClient,
    AmpioConnectionError,
    AmpioObject,
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


def test_mserv_id_prefers_info_mac_cross_check() -> None:
    """mserv_id matches the module whose mac_global matches info.mac."""
    client = _client()
    # Two modules with typ != 10 plus the actual M-SERV (typ=10, mac_global=47846).
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devices",
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
        f"ampio/fromDB/{USER}/data/info",
        info(serverVersion="1", mac="47846"),
    )
    assert client.mserv_id == 1


def test_mserv_id_falls_back_to_typ10_without_info() -> None:
    """Without info, a unique typ_urzadzenia=10 module identifies the M-SERV."""
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devices",
        devices(
            {
                "id": 5,
                "mac": 1,
                "mac_global": 12345,
                "typ_urzadzenia": 10,
                "nazwa_urzadzenia": "MSERV",
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
    assert client.mserv_id == 5


def test_mserv_id_none_when_ambiguous_and_no_info() -> None:
    """If multiple modules are typ=10 and no info reply, do not guess."""
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devices",
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
    assert client.mserv_id is None


def test_state_updates_object_and_notifies() -> None:
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devicesDetails",
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
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    feed(client, topic, details(_flaga(41, 3), _flaga(42, 4)))
    feed(client, topic, details(_flaga(41, 3)))
    assert removed == [42]
    assert 42 not in client.objects

    unsubscribe()
    feed(client, topic, details(_flaga(41, 3), _flaga(42, 4)))
    feed(client, topic, details(_flaga(41, 3)))
    assert removed == [42]


def test_module_removal_dispatches_module_removed() -> None:
    client = _client()
    removed: list[int] = []
    client.subscribe(lambda e: removed.append(e.module.id), of=ModuleRemoved)
    topic = f"ampio/fromDB/{USER}/config/devices"
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
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
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


def test_availability_listener() -> None:
    client = _client()
    events: list[bool] = []
    client.subscribe(lambda e: events.append(e.available), of=AvailabilityChanged)
    client._connection._set_available(True)
    client._connection._set_available(True)
    client._connection._set_available(False)
    assert events == [True, False]


async def test_start_raises_auth_error_on_credential_rejection() -> None:
    """A broker auth rejection during _run surfaces AmpioAuthError from start()."""
    broker = FakeBroker()
    broker.enter_error = aiomqtt.MqttCodeError(
        ReasonCode(PacketTypes.CONNACK, "Not authorized")
    )
    client = AmpioClient(
        "host",
        username=USER,
        password="bad",
        reconnect_interval=0.0,
        mqtt_client_factory=broker.factory,
    )
    with pytest.raises(AmpioAuthError):
        await client.start(timeout=2.0, discovery_timeout=0.1)
    assert client._connection._runner is None  # stop() ran during the raise


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
        await client.start(timeout=0.5, discovery_timeout=0.1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("1", True), ("255", True)],
)
def test_is_on_interpretation(value, expected) -> None:
    assert AmpioObject(id=1, value=value).is_on is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("23.5", 23.5),
        ("255", 255.0),
        ("0", 0.0),
        ("-4.2", -4.2),
        (None, None),
        ("", None),
        ("open", None),
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("1e999", None),
    ],
)
def test_numeric_value_interpretation(value, expected) -> None:
    assert AmpioObject(id=1, value=value).numeric_value == expected


def test_last_payloads_retained_for_each_handler() -> None:
    """Each discovery handler stashes the verbatim payload for diagnostics."""
    client = _client()
    devices_payload = devices({"id": 1, "mac": 1, "typ_urzadzenia": 10})
    details_payload = details(
        {"id": 5, "id_urzadzenia": 1, "typ_komponentu": "temp", "interpretacja": 1}
    )
    info_payload = info(mac=12345, serverVersion="2025")

    feed(client, f"ampio/fromDB/{USER}/config/devices", devices_payload)
    feed(client, f"ampio/fromDB/{USER}/config/devicesDetails", details_payload)
    feed(client, f"ampio/fromDB/{USER}/data/info", info_payload)

    assert client.last_payloads["devices"] == devices_payload.decode()
    assert client.last_payloads["details"] == details_payload.decode()
    assert client.last_payloads["info"] == info_payload.decode()


def test_groups_payloads_are_retained() -> None:
    """`data/groups` and `data/group_devices` populate the last_payloads map."""
    client = _client()
    groups = json.dumps({"List": [{"id": 1, "opis_menu": "Salon"}]}).encode()
    group_devices = json.dumps({"List": [{"id_grupy": 1, "id_obiektu": 5}]}).encode()
    feed(client, f"ampio/fromDB/{USER}/data/groups", groups)
    feed(client, f"ampio/fromDB/{USER}/data/group_devices", group_devices)
    assert client.last_payloads["groups"] == groups.decode()
    assert client.last_payloads["group_devices"] == group_devices.decode()


def test_access_tier_reads_the_account_id_off_the_info_reply() -> None:
    client = _client()
    assert client.access_tier is AccessTier.UNKNOWN
    feed(
        client,
        f"ampio/fromDB/{USER}/data/info",
        b'{"Results": {"mac": 1, "userId": "4"}}',
    )
    assert client.access_tier is AccessTier.RESTRICTED
    feed(
        client,
        f"ampio/fromDB/{USER}/data/info",
        b'{"Results": {"mac": 1, "userId": "-1"}}',
    )
    assert client.access_tier is AccessTier.ADMIN


@pytest.mark.parametrize(
    ("leaf_id", "expected"),
    [("0_cb9b_74_0_1", "leaf_0_cb9b_74_0_1"), ("", None)],
)
def test_stable_key_from_leaf_id(leaf_id: str, expected: str | None) -> None:
    assert AmpioObject(id=1, leaf_id=leaf_id).stable_key == expected


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


@pytest.mark.parametrize(
    ("typ", "leaf_id", "is_system", "visible"),
    [
        # Real object with a non-empty leafId (the real-install shape).
        ("temp", "0_cb8f_76_0_0", False, True),
        # Ghost: empty leafId, not a system type.
        ("temp", "", False, False),
        # Named-output ghost on the M-SERV - the canonical Matter-leak case.
        ("przekaznik", "", False, False),
        # System objects are visible regardless of leafId.
        ("symulacja", "", True, True),
        ("detekcja", "", True, True),
        # `flaga` is an input but NOT a system object, so it needs its leafId.
        ("flaga", "", False, False),
        ("flaga", "0_d09a_3_0_1", False, True),
        # Unclassified / missing typ_komponentu - treat as non-system.
        (None, "", False, False),
        (None, "0_x_x_x_x", False, True),
    ],
)
def test_visibility_predicate(
    typ: str | None,
    leaf_id: str,
    is_system: bool,
    visible: bool,
) -> None:
    obj = AmpioObject(id=1, typ_komponentu=typ, leaf_id=leaf_id)
    assert obj.is_system is is_system
    assert obj.visible is visible


@pytest.mark.parametrize(
    ("params", "hidden"),
    [
        (0, False),  # absent -> no flags
        (1, False),  # bit 0 only (every real object carries it)
        (16, True),  # bit 4 -> hidden stub
        (17, True),  # bit 0 + bit 4 (the live phantom shape)
        (1 << 37, False),  # a Matter opt-in is not a visibility signal
        ((1 << 37) | 16, True),  # opted in AND hidden -> hidden still wins
    ],
)
def test_params_flags(params: int, hidden: bool) -> None:
    obj = AmpioObject(id=1, params=params)
    assert obj.hidden is hidden


def test_hidden_overrides_leaf_id_visibility() -> None:
    """Bit 4 (hidden) drops an object even when its leaf_id would show it.

    This is the duplicated-Designer-channel case: a phantom and its labelled
    twin share a leaf_id, so the leaf_id heuristic keeps both and the consumer's
    unique-id collides. The phantom carries bit 4, so it is filtered out.
    """
    phantom = AmpioObject(
        id=1, typ_komponentu="lin_wej", leaf_id="0_cb97_74_0_1", params=17
    )
    labelled = AmpioObject(
        id=2, typ_komponentu="lin_wej", leaf_id="0_cb97_74_0_1", params=(1 << 37) | 1
    )
    assert phantom.visible is False
    assert labelled.visible is True
    # A system object the M-SERV explicitly hid (bit 4) is dropped too, even
    # though is_system would otherwise force it visible.
    assert AmpioObject(id=3, typ_komponentu="symulacja", params=16).visible is False
