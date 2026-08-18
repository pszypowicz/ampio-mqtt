"""Tests for the AmpioClient surface: listener wiring, lifecycle entry
points, diagnostics retention, and stats. The store's message semantics are
covered in test_store.py; the pure model properties in test_models.py."""

from __future__ import annotations

import asyncio
import json

import aiomqtt
import pytest
from conftest import (
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


# Type 10 is the M-SERV-s; type 0 is VIRTUAL. Both map to Capability.HUB.
@pytest.mark.parametrize("hub_type", [10, 0])
def test_mserv_id_falls_back_to_unique_hub_module(hub_type: int) -> None:
    """Without info, the unique hub-capability module identifies the M-SERV.

    The rule reads Capability.HUB off the module catalogue, not the raw type
    code, so a VIRTUAL hub resolves exactly like an M-SERV one.
    """
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devices",
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

    assert client.last_payloads["devices"] == devices_payload
    assert client.last_payloads["details"] == details_payload
    assert client.last_payloads["info"] == info_payload


def test_groups_payloads_are_retained() -> None:
    """`data/groups` and `data/group_devices` populate the last_payloads map."""
    client = _client()
    groups = json.dumps({"List": [{"id": 1, "opis_menu": "Salon"}]}).encode()
    group_devices = json.dumps({"List": [{"id_grupy": 1, "id_obiektu": 5}]}).encode()
    feed(client, f"ampio/fromDB/{USER}/data/groups", groups)
    feed(client, f"ampio/fromDB/{USER}/data/group_devices", group_devices)
    assert client.last_payloads["groups"] == groups.decode()
    assert client.last_payloads["group_devices"] == group_devices.decode()


def test_access_tier_is_unknown_before_any_info_reply() -> None:
    """The userId-to-tier mapping itself is covered in test_protocol."""
    assert _client().access_tier is AccessTier.UNKNOWN


def test_colliding_macs_surface_through_the_client() -> None:
    client = _client()
    feed(
        client,
        f"ampio/fromDB/{USER}/config/devices",
        devices({"id": 1, "mac": 5}, {"id": 2, "mac": 5}),
    )
    assert client.colliding_macs == frozenset({5})


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


async def test_object_updated_events_are_snapshots(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A deferred listener must see the state the event announced, not
    whatever the object holds by the time it looks."""
    client, _broker = connected
    events: list[ObjectUpdated] = []
    client.subscribe(events.append, of=ObjectUpdated)
    feed(client, f"ampio/fromDB/{USER}/ob/5/state", b'{"state": "1", "on": 2000}')
    feed(client, f"ampio/fromDB/{USER}/ob/5/state", b'{"state": "2", "on": 3000}')
    assert [e.object.value for e in events] == ["1", "2"]
    assert events[0].object is not client.objects[5]


async def test_discovery_stays_incomplete_without_server_identity(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """An info reply without a mac must not complete discovery: a True
    wait promises the identity a consumer scopes its registry by (#78)."""
    client, _broker = connected
    feed(client, STATES_TOPIC, devices())
    feed(client, INFO_TOPIC, info())  # parses, but carries no identity
    feed(client, DATA_DEVICES_TOPIC, details())
    feed(client, PARAMS_DEVICES_TOPIC, devices())
    assert await client.wait_for_initial_discovery(timeout=0.05) is False
    assert client.server_info is not None
    assert client.server_info.key is None

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
        await AmpioClient.test_connection("host", 1883, username, None)
