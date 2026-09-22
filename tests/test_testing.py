"""The fixture seam: build a store, apply a reply, read the events."""

import json

import pytest
from conftest import details, devices, params_of

from ampio_mqtt import (
    AmpioAdminClient,
    AmpioClient,
    ModuleAddress,
    NotConfigured,
    ObjectAdded,
    parse_module_address,
)
from ampio_mqtt.testing import AdminStore, AmpioStore, apply_reply, build_store


def _catalogue(*items: dict) -> tuple[dict, dict]:
    """The decoded pair one catalogue reply is carried by."""
    return json.loads(params_of(*items)), json.loads(details(*items))


def test_build_store_follows_the_client_class() -> None:
    assert type(build_store(AmpioClient)) is AmpioStore
    assert type(build_store(AmpioAdminClient)) is AdminStore


def test_apply_reply_admits_a_row_through_the_real_door() -> None:
    store = build_store(AmpioClient)
    params, catalogue = _catalogue({"id": 41, "opis_menu": "Lamp"})
    assert apply_reply(store, "params_devices", params) == []
    added = apply_reply(store, "data_devices", catalogue)
    assert [type(e) for e in added] == [ObjectAdded]
    assert store.objects[41].name == "Lamp"


def test_apply_reply_refuses_a_leafless_row_the_way_the_wire_does() -> None:
    store = build_store(AmpioClient)
    params, catalogue = _catalogue({"id": 41, "leafId": "", "opis_menu": "Lamp"})
    apply_reply(store, "params_devices", params)
    events = apply_reply(store, "data_devices", catalogue)
    assert events == [NotConfigured(objects=((41, "Lamp"),))]
    assert store.objects == {}


def test_apply_reply_reaches_the_admin_module_list() -> None:
    store = build_store(AmpioAdminClient)
    row = {"id": 2, "mac": 0xB, "mac_global": 102}
    payload = json.loads(devices(row, {**row, "id": 3}))
    assert apply_reply(store, "devices", payload) == [
        NotConfigured(collisions=((0xB, (2, 3)),))
    ]
    assert store.modules == {}


def test_apply_reply_names_an_endpoint_the_library_does_not_serve() -> None:
    with pytest.raises(KeyError):
        apply_reply(build_store(AmpioClient), "no_such_endpoint", {})


def test_parse_module_address_reads_a_leaf_token() -> None:
    assert parse_module_address("0_cb8f_76_0_3") == ModuleAddress(
        mac=0xCB8F, channel=3, sf_id=76, sub_sf_id=0
    )
