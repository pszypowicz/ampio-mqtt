"""Tests for the device_api descriptions wire layer."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from conftest import (
    ADMIN_DETAILS_TOPIC,
    ADMIN_DEVICES_TOPIC,
    ADMIN_USER,
    FakeBroker,
    details,
    devices,
    feed,
)

from ampio_mqtt import (
    AmpioClient,
    AmpioTimeoutError,
    DesignerRecord,
    ModuleRecord,
    ModuleUpdated,
    ObjectUpdated,
)
from ampio_mqtt._protocol import (
    DEVICE_API_LIST_PAYLOAD,
    DEVICE_API_LIST_REQUEST,
    DEVICE_API_LIST_TOPIC,
    ENDPOINTS,
    DeviceList,
    OutputDescription,
    Router,
    parse_descriptions_blob,
    parse_device_list,
    resolve_designer,
    resolve_module_records,
)
from ampio_mqtt.models import AmpioObject

LOCATIONS_TOPIC = f"ampio/fromDB/{ADMIN_USER}/config/locations"


def frame(desc_type: int, out_no: int, out_loc: int, out_type: int, desc: str) -> bytes:
    body = desc.encode()
    length = 10 + len(body)
    return (
        b"".join(
            v.to_bytes(2, "little")
            for v in (length, desc_type, out_no, out_loc, out_type)
        )
        + body
    )


def _device(
    mac_prod: int, mac_user: int, *frames: bytes, blob: str | None = None
) -> dict:
    """One `device_api/from/list` device entry; `blob` overrides the encoding."""
    row: dict = {"macProd": mac_prod, "macUser": mac_user}
    if blob is not None:
        row["descriptions"] = blob
    elif frames:
        row["descriptions"] = base64.b64encode(b"".join(frames)).decode()
    return row


def _list(*devs: object) -> str:
    return json.dumps({"devices": list(devs)})


def test_blob_decodes_frames_in_order() -> None:
    blob = frame(12, 0, 14, 256, "Lampa") + frame(26, 1, 19, 514, "Roleta")
    assert parse_descriptions_blob(blob) == (
        OutputDescription(
            desc_type=12, out_no=0, out_loc=14, out_type=256, desc="Lampa"
        ),
        OutputDescription(
            desc_type=26, out_no=1, out_loc=19, out_type=514, desc="Roleta"
        ),
    )


def test_blob_stops_on_short_or_overrunning_length() -> None:
    assert parse_descriptions_blob(frame(12, 0, 0, 0, "ok") + b"\x02\x00") == (
        OutputDescription(desc_type=12, out_no=0, out_loc=0, out_type=0, desc="ok"),
    )
    truncated = frame(12, 0, 0, 0, "long description")[:-4]
    assert parse_descriptions_blob(truncated) == ()


def test_blob_stops_when_length_field_is_below_header_size() -> None:
    # 10 bytes available (clears the outer while guard) but the length field
    # itself reads 2, below the 10-byte header - length < 10 is the clause
    # that must decide here, not a truncated remainder.
    blob = (2).to_bytes(2, "little") + bytes(8)
    assert parse_descriptions_blob(blob) == ()


def test_blob_decodes_zero_body_frame_at_length_boundary() -> None:
    blob = frame(12, 0, 14, 256, "") + frame(26, 1, 19, 514, "Roleta")
    assert parse_descriptions_blob(blob) == (
        OutputDescription(desc_type=12, out_no=0, out_loc=14, out_type=256, desc=""),
        OutputDescription(
            desc_type=26, out_no=1, out_loc=19, out_type=514, desc="Roleta"
        ),
    )


def test_device_list_keys_each_device_by_both_ids() -> None:
    devs = parse_device_list(
        _list(
            _device(0xBAE6, 1, frame(12, 0, 1, 266, "kropelki")),
            _device(0xCB89, 0xCB89, frame(12, 0, 14, 256, "L")),
        )
    )
    assert devs is not None
    assert [(d.mac, d.mac_global) for d in devs] == [(1, 0xBAE6), (0xCB89, 0xCB89)]
    assert devs[0].entries[0].out_loc == 1
    assert devs[1].entries[0].desc == "L"


def test_device_list_without_descriptions_reads_empty() -> None:
    devs = parse_device_list(_list(_device(1, 1), _device(2, 2, blob="")))
    assert devs is not None
    assert [d.entries for d in devs] == [(), ()]


def test_device_list_skips_unreadable_devices() -> None:
    devs = parse_device_list(
        _list(
            _device(1, 1, blob="!!!not-base64"),
            {"macProd": 2, "descriptions": ""},
            {"macProd": 3, "macUser": "zz"},
            "not-a-device",
            _device(4, 4, frame(12, 0, 14, 256, "L")),
        )
    )
    assert devs is not None
    assert [(d.mac, d.mac_global) for d in devs] == [(4, 4)]


def test_device_list_rejects_garbage() -> None:
    assert parse_device_list("not-json") is None
    assert parse_device_list(json.dumps([1, 2])) is None
    assert parse_device_list(json.dumps({"devices": 5})) is None
    assert parse_device_list(json.dumps({})) is None


def test_router_routes_the_list_reply() -> None:
    router = Router("admin", ENDPOINTS)
    msg = router.route(
        DEVICE_API_LIST_TOPIC, _list(_device(0xCB89, 0xCB89, frame(12, 2, 3, 0, "x")))
    )
    assert isinstance(msg, DeviceList)
    assert msg.devices[0].mac == 0xCB89
    assert msg.devices[0].entries[0].out_no == 2
    assert router.route(DEVICE_API_LIST_TOPIC, "not-json") is None


def test_list_request_pair_is_the_designer_s() -> None:
    assert DEVICE_API_LIST_REQUEST == "device_api/to/list"
    assert DEVICE_API_LIST_PAYLOAD == b"0"
    assert DEVICE_API_LIST_TOPIC == "device_api/from/list"


def _entries(*specs: tuple[int, int, int, int, str]) -> tuple[OutputDescription, ...]:
    return tuple(OutputDescription(*s) for s in specs)


def test_resolve_designer_joins_location_and_type() -> None:
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
        48: AmpioObject(
            id=48, typ_komponentu="roleta_procenty", leaf_id="0_cb89_5_0_1"
        ),
    }
    by_mac = {
        0xCB89: _entries((12, 0, 14, 256, "Lampa"), (26, 1, 0, 0, "Roleta")),
    }
    resolved = resolve_designer(objects, by_mac, {14: "Potter"}, frozenset(), {})
    assert resolved == {
        64: DesignerRecord(location="Potter", matter_device_type=256, desc="Lampa"),
        48: DesignerRecord(location=None, matter_device_type=None, desc="Roleta"),
    }


def test_resolve_designer_skips_the_unjoinable() -> None:
    objects = {
        1: AmpioObject(id=1, typ_komponentu="flaga_x", leaf_id="0_cb89_3_0_0"),
        2: AmpioObject(id=2, typ_komponentu="przekaznik", leaf_id=""),
        3: AmpioObject(id=3, typ_komponentu="przekaznik", leaf_id="0_beef_257_2_0"),
        4: AmpioObject(id=4, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_9"),
    }
    by_mac = {0xCB89: _entries((12, 0, 14, 256, "L"))}
    assert resolve_designer(objects, by_mac, {14: "P"}, frozenset(), {}) == {}


def test_resolve_designer_skips_colliding_macs() -> None:
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
    }
    by_mac = {0xCB89: _entries((12, 0, 14, 256, "L"))}
    assert resolve_designer(objects, by_mac, {14: "P"}, frozenset({0xCB89}), {}) == {}


def test_resolve_designer_reads_empty_desc_as_none() -> None:
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
    }
    by_mac = {0xCB89: _entries((12, 0, 0, 0, ""))}
    assert resolve_designer(objects, by_mac, {}, frozenset(), {}) == {
        64: DesignerRecord(location=None, matter_device_type=None, desc=None)
    }


def test_resolve_designer_reads_clear_sentinels_as_none() -> None:
    """A cleared Designer entry (outLoc 16383, desc ".") reads all-None,
    even when the names table carries the sentinel id."""
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
    }
    by_mac = {0xCB89: _entries((12, 0, 16383, 0, "."))}
    assert resolve_designer(objects, by_mac, {16383: "Bogus"}, frozenset(), {}) == {
        64: DesignerRecord(location=None, matter_device_type=None, desc=None)
    }


def test_resolve_designer_joins_a_flag_on_the_binary_flag_class() -> None:
    objects = {152: AmpioObject(id=152, typ_komponentu="flaga", leaf_id="0_1_3_0_0")}
    by_mac = {1: _entries((6, 0, 19, 21, "flag"), (12, 0, 1, 266, "relay"))}
    assert resolve_designer(objects, by_mac, {19: "Testowe"}, frozenset(), {}) == {
        152: DesignerRecord(location="Testowe", matter_device_type=21, desc="flag")
    }


def test_resolve_designer_joins_a_leafless_object_through_funkcja() -> None:
    """No leaf: the module comes from id_urzadzenia and the channel from
    funkcja - 1, the relation every leafed object of the table kinds holds."""
    objects = {
        153: AmpioObject(
            id=153, typ_komponentu="flaga", id_urzadzenia=1, funkcja=2, leaf_id=""
        ),
        143: AmpioObject(
            id=143, typ_komponentu="przekaznik", id_urzadzenia=3, funkcja=1, leaf_id=""
        ),
    }
    by_mac = {
        1: _entries((6, 1, 19, 21, "test2")),
        0xBE82: _entries((12, 0, 19, 266, "Test Switch")),
    }
    resolved = resolve_designer(
        objects, by_mac, {19: "Testowe"}, frozenset(), {1: 1, 3: 0xBE82}
    )
    assert resolved == {
        153: DesignerRecord(location="Testowe", matter_device_type=21, desc="test2"),
        143: DesignerRecord(
            location="Testowe", matter_device_type=266, desc="Test Switch"
        ),
    }


def test_resolve_designer_skips_a_leafless_object_without_a_module() -> None:
    objects = {
        153: AmpioObject(
            id=153, typ_komponentu="flaga", id_urzadzenia=9, funkcja=2, leaf_id=""
        ),
        154: AmpioObject(id=154, typ_komponentu="flaga", funkcja=2, leaf_id=""),
        155: AmpioObject(id=155, typ_komponentu="flaga", id_urzadzenia=1, leaf_id=""),
    }
    by_mac = {1: _entries((6, 1, 19, 21, "test2"))}
    assert resolve_designer(objects, by_mac, {19: "Testowe"}, frozenset(), {1: 1}) == {}


def test_resolve_module_records_reads_the_device_name_entry() -> None:
    by_mac = {
        0xCB89: _entries((1, 0, 14, 0, "Modul"), (12, 0, 19, 256, "Lampa")),
        0xBEEF: _entries((12, 0, 19, 256, "L")),  # no DEVICE_NAME entry
        0xCAFE: _entries((1, 0, 0, 0, "M")),  # DEVICE_NAME with outLoc 0
    }
    names = {14: "Rozdzielnia", 19: "Salon"}
    assert resolve_module_records(by_mac, names, frozenset()) == {
        0xCB89: ModuleRecord(location="Rozdzielnia", desc="Modul"),
        0xBEEF: ModuleRecord(),
        0xCAFE: ModuleRecord(location=None, desc="M"),
    }


def test_resolve_module_records_reads_clear_sentinels_as_none() -> None:
    by_mac = {0xCB89: _entries((1, 0, 16383, 0, "."))}
    assert resolve_module_records(by_mac, {16383: "Bogus"}, frozenset()) == {
        0xCB89: ModuleRecord(location=None, desc=None)
    }


def test_resolve_module_records_skips_colliding_macs() -> None:
    by_mac = {0xCB89: _entries((1, 0, 14, 0, "M"))}
    names = {14: "Rozdzielnia"}
    assert resolve_module_records(by_mac, names, frozenset({0xCB89})) == {}


async def _admin_client_with_catalogue() -> tuple[AmpioClient, FakeBroker]:
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.connect(timeout=2.0, discovery_timeout=0.01)
    feed(
        client,
        ADMIN_DETAILS_TOPIC,
        details({"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"}),
    )
    feed(client, ADMIN_DEVICES_TOPIC, devices({"id": 16, "mac": 0xCB89}))
    broker.published.clear()
    return client, broker


async def _deliver_causally(
    client: AmpioClient,
    broker: FakeBroker,
    locations_payload: str,
    list_payload: str | None,
) -> None:
    """Feed each reply only after its request was published, as the broker would."""
    async with asyncio.timeout(1.0):
        while (
            f"ampio/control/{ADMIN_USER}/config",
            b"locations",
        ) not in broker.published:
            await asyncio.sleep(0)
        feed(client, LOCATIONS_TOPIC, locations_payload)
        while (
            DEVICE_API_LIST_REQUEST,
            DEVICE_API_LIST_PAYLOAD,
        ) not in broker.published:
            await asyncio.sleep(0)
        if list_payload is not None:
            feed(client, DEVICE_API_LIST_TOPIC, list_payload)


async def test_admin_subscribes_the_device_api_list_topic() -> None:
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.connect(timeout=2.0, discovery_timeout=0.01)
    try:
        assert DEVICE_API_LIST_TOPIC in broker.subscribed
    finally:
        await client.disconnect()
    restricted_broker = FakeBroker()
    restricted = AmpioClient(
        "host", username="u", mqtt_client_factory=restricted_broker.factory
    )
    await restricted.connect(timeout=2.0, discovery_timeout=0.01)
    try:
        assert DEVICE_API_LIST_TOPIC not in restricted_broker.subscribed
    finally:
        await restricted.disconnect()


async def test_resolve_records_reads_the_list_joins_and_merges() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        events: list[ObjectUpdated] = []
        client.subscribe(events.append, of=ObjectUpdated, object_id=64)
        module_events: list[ModuleUpdated] = []
        client.subscribe(module_events.append, of=ModuleUpdated)
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps(
                    {
                        "List": [
                            {"id": 14, "opis_menu": "Potter"},
                            {"id": 19, "opis_menu": "Rozdzielnia"},
                        ]
                    }
                ),
                _list(
                    _device(
                        0xCB89,
                        0xCB89,
                        frame(1, 0, 19, 0, "Modul"),
                        frame(12, 0, 14, 256, "L"),
                    )
                ),
            )
        )
        try:
            result = await client.resolve_records(timeout=1.0)
        finally:
            await delivery
        assert result.records == {
            64: DesignerRecord(location="Potter", matter_device_type=256, desc="L")
        }
        assert result.answered_macs == frozenset({0xCB89})
        assert result.silent_macs == frozenset()
        assert client.objects[64].record == DesignerRecord(
            location="Potter", matter_device_type=256, desc="L"
        )
        assert client.objects[64].matter_device_type is None
        assert (DEVICE_API_LIST_REQUEST, DEVICE_API_LIST_PAYLOAD) in broker.published
        assert [e.object.record.location for e in events] == ["Potter"]
        assert client.modules[16].record == ModuleRecord(
            location="Rozdzielnia", desc="Modul"
        )
        assert [m.module.record.location for m in module_events] == ["Rozdzielnia"]
    finally:
        await client.disconnect()


async def test_resolve_records_reports_catalogue_modules_absent_from_the_list() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        feed(
            client,
            ADMIN_DEVICES_TOPIC,
            devices({"id": 16, "mac": 0xCB89}, {"id": 17, "mac": 0xBEEF}),
        )
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps({"List": [{"id": 14, "opis_menu": "Potter"}]}),
                _list(_device(0xCB89, 0xCB89, frame(12, 0, 14, 256, "L"))),
            )
        )
        try:
            result = await client.resolve_records(timeout=0.2)
        finally:
            await delivery
        assert result.records == {
            64: DesignerRecord(location="Potter", matter_device_type=256, desc="L")
        }
        assert result.answered_macs == frozenset({0xCB89})
        assert result.silent_macs == frozenset({0xBEEF})
    finally:
        await client.disconnect()


async def test_resolve_records_joins_by_the_override_mac_the_reply_carries() -> None:
    """The M-SERV shape: factory id and override differ, and the leaf embeds
    the override."""
    client, broker = await _admin_client_with_catalogue()
    try:
        feed(
            client,
            ADMIN_DETAILS_TOPIC,
            details(
                {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"},
                {"id": 113, "typ_komponentu": "przekaznik", "leafId": "0_1_257_2_0"},
            ),
        )
        feed(
            client,
            ADMIN_DEVICES_TOPIC,
            devices(
                {"id": 16, "mac": 0xCB89, "mac_global": 0xCB89},
                {"id": 1, "mac": 1, "mac_global": 0xBAE6},
            ),
        )
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps({"List": [{"id": 1, "opis_menu": "Ogrod"}]}),
                _list(
                    _device(0xCB89, 0xCB89),
                    _device(0xBAE6, 1, frame(12, 0, 1, 266, "kropelki")),
                ),
            )
        )
        try:
            result = await client.resolve_records(timeout=0.2)
        finally:
            await delivery
        assert result.records == {
            113: DesignerRecord(
                location="Ogrod", matter_device_type=266, desc="kropelki"
            )
        }
        assert result.answered_macs == frozenset({0xCB89, 1})
        assert result.silent_macs == frozenset()
        assert client.modules[1].record == ModuleRecord()
    finally:
        await client.disconnect()


async def test_resolve_records_joins_a_leafless_object_through_the_catalogue() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        feed(
            client,
            ADMIN_DETAILS_TOPIC,
            details(
                {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"},
                {
                    "id": 65,
                    "typ_komponentu": "przekaznik",
                    "id_urzadzenia": 16,
                    "funkcja": 2,
                },
            ),
        )
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps({"List": [{"id": 14, "opis_menu": "Potter"}]}),
                _list(_device(0xCB89, 0xCB89, frame(12, 1, 14, 256, "Second"))),
            )
        )
        try:
            result = await client.resolve_records(timeout=0.2)
        finally:
            await delivery
        assert result.records == {
            65: DesignerRecord(location="Potter", matter_device_type=256, desc="Second")
        }
        assert client.objects[65].record is not None
    finally:
        await client.disconnect()


async def test_resolve_records_counts_an_empty_record_as_answered() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps({"List": []}),
                _list(_device(0xCB89, 0xCB89, blob="")),
            )
        )
        try:
            result = await client.resolve_records(timeout=0.2)
        finally:
            await delivery
        assert result.records == {}
        assert result.answered_macs == frozenset({0xCB89})
        assert result.silent_macs == frozenset()
        assert client.objects[64].record is None
    finally:
        await client.disconnect()


async def test_resolve_records_raises_when_the_list_never_answers() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        delivery = asyncio.create_task(
            _deliver_causally(client, broker, json.dumps({"List": []}), None)
        )
        try:
            with pytest.raises(AmpioTimeoutError, match="module records"):
                await client.resolve_records(timeout=0.2)
        finally:
            await delivery
        assert client.objects[64].record is None
    finally:
        await client.disconnect()


async def test_resolve_records_raises_on_restricted_tier() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.connect(timeout=2.0, discovery_timeout=0.01)
    try:
        with pytest.raises(RuntimeError, match="admin"):
            await client.resolve_records(timeout=0.1)
    finally:
        await client.disconnect()
