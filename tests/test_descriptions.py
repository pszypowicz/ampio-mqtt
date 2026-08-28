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
    DesignerRecord,
    ModuleRecord,
    ModuleUpdated,
    ObjectUpdated,
)
from ampio_mqtt._protocol import (
    ENDPOINTS,
    DeviceDescriptions,
    OutputDescription,
    Router,
    device_api_request_topic,
    parse_descriptions_blob,
    parse_device_info,
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


def test_device_info_extracts_descriptions() -> None:
    payload = json.dumps(
        {
            "macProd": 52105,
            "descriptions": base64.b64encode(frame(12, 0, 14, 256, "L")).decode(),
        }
    )
    entries = parse_device_info(payload)
    assert entries is not None and entries[0].out_loc == 14


def test_device_info_without_descriptions_reads_empty() -> None:
    assert parse_device_info(json.dumps({"macProd": 1})) == ()
    assert parse_device_info(json.dumps({"descriptions": ""})) == ()


def test_device_info_rejects_garbage() -> None:
    assert parse_device_info("not-json") is None
    assert parse_device_info(json.dumps({"descriptions": "!!!not-base64"})) is None
    assert parse_device_info(json.dumps({"descriptions": 5})) is None


def test_device_info_rejects_non_object_json() -> None:
    assert parse_device_info(json.dumps([1, 2])) is None


def test_router_routes_info_reply_case_insensitively() -> None:
    router = Router("admin", ENDPOINTS)
    payload = json.dumps(
        {"descriptions": base64.b64encode(frame(12, 2, 3, 0, "x")).decode()}
    )
    msg = router.route("device_api/from/CB89/info", payload)
    assert isinstance(msg, DeviceDescriptions)
    assert msg.mac == 0xCB89
    assert msg.entries[0].out_no == 2
    assert router.route("device_api/from/zz/info", payload) is None
    assert router.route("device_api/from/CB89/info", "not-json") is None


def test_request_topic_uses_lowercase_hex() -> None:
    assert device_api_request_topic(0xCB89) == "device_api/to/cb89/get_data"


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
    resolved = resolve_designer(objects, by_mac, {14: "Potter"}, frozenset())
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
    assert resolve_designer(objects, by_mac, {14: "P"}, frozenset()) == {}


def test_resolve_designer_skips_colliding_macs() -> None:
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
    }
    by_mac = {0xCB89: _entries((12, 0, 14, 256, "L"))}
    assert resolve_designer(objects, by_mac, {14: "P"}, frozenset({0xCB89})) == {}


def test_resolve_designer_reads_empty_desc_as_none() -> None:
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
    }
    by_mac = {0xCB89: _entries((12, 0, 0, 0, ""))}
    assert resolve_designer(objects, by_mac, {}, frozenset()) == {
        64: DesignerRecord(location=None, matter_device_type=None, desc=None)
    }


def test_resolve_designer_reads_clear_sentinels_as_none() -> None:
    """A cleared Designer entry (outLoc 16383, desc ".") reads all-None,
    even when the names table carries the sentinel id."""
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
    }
    by_mac = {0xCB89: _entries((12, 0, 16383, 0, "."))}
    assert resolve_designer(objects, by_mac, {16383: "Bogus"}, frozenset()) == {
        64: DesignerRecord(location=None, matter_device_type=None, desc=None)
    }


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
    info_replies: list[tuple[str, str]],
) -> None:
    """Feed each reply only after its request was published, as the broker would."""
    async with asyncio.timeout(1.0):
        while (
            f"ampio/control/{ADMIN_USER}/config",
            b"locations",
        ) not in broker.published:
            await asyncio.sleep(0)
        feed(client, LOCATIONS_TOPIC, locations_payload)
        while ("device_api/to/cb89/get_data", b"") not in broker.published:
            await asyncio.sleep(0)
        for topic, payload in info_replies:
            feed(client, topic, payload)


async def test_admin_subscribes_the_device_api_wildcard() -> None:
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.connect(timeout=2.0, discovery_timeout=0.01)
    try:
        assert "device_api/from/+/info" in broker.subscribed
    finally:
        await client.disconnect()
    restricted_broker = FakeBroker()
    restricted = AmpioClient(
        "host", username="u", mqtt_client_factory=restricted_broker.factory
    )
    await restricted.connect(timeout=2.0, discovery_timeout=0.01)
    try:
        assert "device_api/from/+/info" not in restricted_broker.subscribed
    finally:
        await restricted.disconnect()


async def test_resolve_records_sweeps_joins_and_merges() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        events: list[ObjectUpdated] = []
        client.subscribe(events.append, of=ObjectUpdated, object_id=64)
        module_events: list[ModuleUpdated] = []
        client.subscribe(module_events.append, of=ModuleUpdated)
        info_payload = json.dumps(
            {
                "descriptions": base64.b64encode(
                    frame(1, 0, 19, 0, "Modul") + frame(12, 0, 14, 256, "L")
                ).decode()
            }
        )
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
                [("device_api/from/CB89/info", info_payload)],
            )
        )
        try:
            result = await client.resolve_records(timeout=1.0)
        finally:
            await delivery
        assert result == {
            64: DesignerRecord(location="Potter", matter_device_type=256, desc="L")
        }
        assert client.objects[64].record == DesignerRecord(
            location="Potter", matter_device_type=256, desc="L"
        )
        assert client.objects[64].matter_device_type is None
        assert ("device_api/to/cb89/get_data", b"") in broker.published
        assert [e.object.record.location for e in events] == ["Potter"]
        assert client.modules[16].record == ModuleRecord(
            location="Rozdzielnia", desc="Modul"
        )
        assert [m.module.record.location for m in module_events] == ["Rozdzielnia"]
    finally:
        await client.disconnect()


async def test_resolve_records_tolerates_silent_modules() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        feed(
            client,
            ADMIN_DEVICES_TOPIC,
            devices({"id": 16, "mac": 0xCB89}, {"id": 17, "mac": 0xBEEF}),
        )
        info_payload = json.dumps(
            {"descriptions": base64.b64encode(frame(12, 0, 14, 256, "L")).decode()}
        )
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps({"List": [{"id": 14, "opis_menu": "Potter"}]}),
                [("device_api/from/CB89/info", info_payload)],
            )
        )
        try:
            result = await client.resolve_records(timeout=0.2)
        finally:
            await delivery
        assert result == {
            64: DesignerRecord(location="Potter", matter_device_type=256, desc="L")
        }
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
