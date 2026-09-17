"""Tests for the device_api descriptions wire layer."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from conftest import (
    ADMIN_DATA_DEVICES_TOPIC,
    ADMIN_DEVICES_TOPIC,
    ADMIN_PARAMS_DEVICES_TOPIC,
    ADMIN_USER,
    FakeBroker,
    details,
    devices,
    feed,
    params_of,
)

from ampio_mqtt import (
    AmpioAdminClient,
    AmpioClient,
    AmpioTimeoutError,
    DesignerRecord,
    ModuleFunction,
    ModuleRecord,
    ModuleUpdated,
    ObjectUpdated,
    RecordSweepCompleted,
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
    resolve_module_capabilities,
    resolve_module_records,
)
from ampio_mqtt.models import AmpioObject, ModuleAddress


def _object(**over: object) -> AmpioObject:
    """An object carrying the catalogue columns every row serves."""
    row: dict[str, object] = {
        "id": 1,
        "typ_komponentu": "",
        "interpretacja": 0,
        "funkcja": 1,
        "address": ModuleAddress(mac=0xCAFE, channel=0, sf_id=257, sub_sf_id=0),
        "leaf_key": "leaf_0_cafe_257_0_0",
    }
    return AmpioObject(**{**row, **over})  # type: ignore[arg-type]


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


def caps(*pairs: tuple[int, int]) -> str:
    """A `supportedFunctions` blob: 2 bytes per (function id, channel count)."""
    return base64.b64encode(bytes(b for pair in pairs for b in pair)).decode()


def panel_params(fields: int) -> str:
    """A params blob for a `fields`-field panel, the live baseline values."""
    mask_len = -(-fields // 8)
    every_field = ((1 << fields) - 1).to_bytes(mask_len, "little")
    blob = bytes([0, 0, 0, 255, 255, 10, 10])  # colours
    blob += bytes([1] * fields) + bytes([2])  # light signal, beep time
    blob += every_field * 2  # sound, backlight
    blob += bytes(mask_len) + bytes([0])  # multitouch mask, send mode
    blob += bytes([10, 50])  # dim after 10 s to 50 %
    return base64.b64encode(blob).decode()


def roller_params() -> str:
    """A params blob whose roller section is the one-channel M-REL-2 layout."""
    section = bytes.fromhex(
        "00"  # work mode: plain
        "2800"  # opening: 40 s
        "2800"  # closing: 40 s
        "0A"  # calibration: 10 %
        "6400"  # slat movement: 100 ticks
        "32"  # reversal lag: 50 ticks
        "00"  # unlabeled
        "14"  # start lag, same direction: 20 ticks
        "0C"  # start lag, other direction: 12 ticks
    )
    return base64.b64encode(bytes(33) + section).decode()


def _device(
    mac_prod: int,
    mac_user: int,
    *frames: bytes,
    blob: str | None = None,
    functions: str | None = None,
    params: str | None = None,
) -> dict:
    """One `device_api/from/list` device entry; `blob` overrides the encoding."""
    row: dict = {"macProd": mac_prod, "macUser": mac_user}
    if blob is not None:
        row["descriptions"] = blob
    elif frames:
        row["descriptions"] = base64.b64encode(b"".join(frames)).decode()
    if functions is not None:
        row["supportedFunctions"] = functions
    if params is not None:
        row["params"] = params
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


def test_device_list_reads_the_capability_pairs() -> None:
    devs = parse_device_list(
        _list(
            _device(
                1,
                1,
                functions=caps(
                    (ModuleFunction.IN_BIN, 18),
                    (ModuleFunction.BACKLIGHT_RGBW, 18),
                    (ModuleFunction.KEY_LOCK, 1),
                ),
            )
        )
    )
    assert devs is not None
    assert devs[0].capabilities == {
        ModuleFunction.IN_BIN: 18,
        ModuleFunction.BACKLIGHT_RGBW: 18,
        ModuleFunction.KEY_LOCK: 1,
    }
    # An id this library cannot name still reads through under its number.
    devs = parse_device_list(_list(_device(1, 1, functions=caps((253, 4)))))
    assert devs is not None
    assert devs[0].capabilities == {253: 4}


def test_device_list_unreadable_capabilities_keep_the_device() -> None:
    """Capabilities are additive, so a bad blob must not cost the record."""
    devs = parse_device_list(
        _list(
            _device(1, 1, frame(12, 0, 14, 256, "L")),  # field absent
            _device(2, 2, functions="!!!not-base64"),
            _device(3, 3, functions=base64.b64encode(bytes([7])).decode()),  # odd
            _device(4, 4, functions=""),
        )
    )
    assert devs is not None
    assert [(d.mac, d.capabilities) for d in devs] == [
        (1, {}),
        (2, {}),
        (3, {}),
        (4, {}),
    ]
    assert devs[0].entries[0].desc == "L"


def test_device_list_last_capability_pair_wins() -> None:
    """A repeated id is not expected on the wire; pick one rule and hold it."""
    devs = parse_device_list(
        _list(
            _device(
                1, 1, functions=caps((ModuleFunction.OW, 6), (ModuleFunction.OW, 2))
            )
        )
    )
    assert devs is not None
    assert devs[0].capabilities == {ModuleFunction.OW: 2}


def test_device_list_rejects_garbage() -> None:
    assert parse_device_list("not-json") is None
    assert parse_device_list(json.dumps([1, 2])) is None
    assert parse_device_list(json.dumps({"devices": 5})) is None
    assert parse_device_list(json.dumps({})) is None


def test_router_routes_the_list_reply() -> None:
    router = Router("admin", ENDPOINTS, admin=True)
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
        64: _object(
            id=64,
            typ_komponentu="przekaznik",
            address=ModuleAddress(mac=0xCB89, channel=0, sf_id=257, sub_sf_id=2),
        ),
        48: _object(
            id=48,
            typ_komponentu="roleta_procenty",
            address=ModuleAddress(mac=0xCB89, channel=1, sf_id=5, sub_sf_id=0),
        ),
    }
    by_mac = {
        0xCB89: _entries((12, 0, 14, 256, "Lampa"), (26, 1, 0, 0, "Roleta")),
    }
    resolved = resolve_designer(objects, by_mac, {14: "Potter"})
    assert resolved == {
        64: DesignerRecord(location="Potter", matter_device_type=256, desc="Lampa"),
        48: DesignerRecord(location=None, matter_device_type=None, desc="Roleta"),
    }


def test_resolve_designer_skips_the_unjoinable() -> None:
    objects = {
        1: _object(
            id=1,
            typ_komponentu="flaga_x",
            address=ModuleAddress(mac=0xCB89, channel=0, sf_id=3, sub_sf_id=0),
        ),
        3: _object(
            id=3,
            typ_komponentu="przekaznik",
            address=ModuleAddress(mac=0xBEEF, channel=0, sf_id=257, sub_sf_id=2),
        ),
        4: _object(
            id=4,
            typ_komponentu="przekaznik",
            address=ModuleAddress(mac=0xCB89, channel=9, sf_id=257, sub_sf_id=2),
        ),
    }
    by_mac = {0xCB89: _entries((12, 0, 14, 256, "L"))}
    assert resolve_designer(objects, by_mac, {14: "P"}) == {}


def test_resolve_designer_reads_empty_desc_as_none() -> None:
    objects = {
        64: _object(
            id=64,
            typ_komponentu="przekaznik",
            address=ModuleAddress(mac=0xCB89, channel=0, sf_id=257, sub_sf_id=2),
        ),
    }
    by_mac = {0xCB89: _entries((12, 0, 0, 0, ""))}
    assert resolve_designer(objects, by_mac, {}) == {
        64: DesignerRecord(location=None, matter_device_type=None, desc=None)
    }


def test_resolve_designer_reads_clear_sentinels_as_none() -> None:
    """A cleared Designer entry (outLoc 16383, desc ".") reads all-None,
    even when the names table carries the sentinel id."""
    objects = {
        64: _object(
            id=64,
            typ_komponentu="przekaznik",
            address=ModuleAddress(mac=0xCB89, channel=0, sf_id=257, sub_sf_id=2),
        ),
    }
    by_mac = {0xCB89: _entries((12, 0, 16383, 0, "."))}
    assert resolve_designer(objects, by_mac, {16383: "Bogus"}) == {
        64: DesignerRecord(location=None, matter_device_type=None, desc=None)
    }


def test_resolve_designer_joins_a_flag_on_the_binary_flag_class() -> None:
    objects = {
        152: _object(
            id=152,
            typ_komponentu="flaga",
            address=ModuleAddress(mac=1, channel=0, sf_id=3, sub_sf_id=0),
        )
    }
    by_mac = {1: _entries((6, 0, 19, 21, "flag"), (12, 0, 1, 266, "relay"))}
    assert resolve_designer(objects, by_mac, {19: "Testowe"}) == {
        152: DesignerRecord(location="Testowe", matter_device_type=21, desc="flag")
    }


def test_resolve_module_records_reads_the_device_name_entry() -> None:
    by_mac = {
        0xCB89: _entries((1, 0, 14, 0, "Modul"), (12, 0, 19, 256, "Lampa")),
        0xBEEF: _entries((12, 0, 19, 256, "L")),  # no DEVICE_NAME entry
        0xCAFE: _entries((1, 0, 0, 0, "M")),  # DEVICE_NAME with outLoc 0
    }
    names = {14: "Rozdzielnia", 19: "Salon"}
    assert resolve_module_records(by_mac, names) == {
        0xCB89: ModuleRecord(location="Rozdzielnia", desc="Modul"),
        0xBEEF: ModuleRecord(),
        0xCAFE: ModuleRecord(location=None, desc="M"),
    }


def test_resolve_module_records_reads_clear_sentinels_as_none() -> None:
    by_mac = {0xCB89: _entries((1, 0, 16383, 0, "."))}
    assert resolve_module_records(by_mac, {16383: "Bogus"}) == {
        0xCB89: ModuleRecord(location=None, desc=None)
    }


def test_resolve_module_capabilities_keys_by_mac() -> None:
    by_mac = {
        0xCB89: {ModuleFunction.BACKLIGHT_RGBW: 18},
        0xBEEF: {},
        0xCAFE: {ModuleFunction.BUZZER: 1},
    }
    assert resolve_module_capabilities(by_mac) == by_mac


async def _admin_client_with_catalogue() -> tuple[AmpioAdminClient, FakeBroker]:
    broker = FakeBroker()
    client = AmpioAdminClient("host", mqtt_client_factory=broker.factory)
    await client.connect(timeout=2.0, discovery_timeout=0.01)
    feed(
        client,
        ADMIN_PARAMS_DEVICES_TOPIC,
        params_of(
            {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"}
        ),
    )
    feed(
        client,
        ADMIN_DATA_DEVICES_TOPIC,
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
    client = AmpioAdminClient("host", mqtt_client_factory=broker.factory)
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
                        functions=caps(
                            (ModuleFunction.BACKLIGHT_RGBW, 18),
                            (ModuleFunction.KEY_LOCK, 1),
                        ),
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
        assert client.records[64] == DesignerRecord(
            location="Potter", matter_device_type=256, desc="L"
        )
        assert client.objects[64].matter_device_type is None
        assert (DEVICE_API_LIST_REQUEST, DEVICE_API_LIST_PAYLOAD) in broker.published
        assert client.module_records[0xCB89] == ModuleRecord(
            location="Rozdzielnia", desc="Modul"
        )
        # The datasets sit beside the models, so no object and no module
        # changed.
        assert events == []
        assert module_events == []
        # The same sweep fills the capability map - no extra request.
        assert client.capabilities[0xCB89] == {
            ModuleFunction.BACKLIGHT_RGBW: 18,
            ModuleFunction.KEY_LOCK: 1,
        }
    finally:
        await client.disconnect()


async def test_resolve_records_fills_the_datasets_and_fires_once() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        # An M-REL-2: a board whose roller layout the library has proven,
        # with a cover of its own beside the relay the fixture catalogues.
        rows = (
            {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"},
            {"id": 70, "typ_komponentu": "roleta_procenty", "leafId": "0_cb89_5_0_0"},
        )
        feed(client, ADMIN_PARAMS_DEVICES_TOPIC, params_of(*rows))
        feed(client, ADMIN_DATA_DEVICES_TOPIC, details(*rows))
        feed(
            client,
            ADMIN_DEVICES_TOPIC,
            devices({"id": 16, "mac": 0xCB89, "typ_urzadzenia": 24, "wersja_pcb": 11}),
        )
        events: list[RecordSweepCompleted] = []
        client.subscribe(events.append, of=RecordSweepCompleted)
        assert client.last_sweep is None
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps({"List": [{"id": 14, "opis_menu": "Hall"}]}),
                _list(
                    _device(
                        0xCB89,
                        0xCB89,
                        frame(12, 0, 14, 0, "x"),
                        functions=caps((ModuleFunction.ROLLER, 1)),
                        params=roller_params(),
                    )
                ),
            )
        )
        try:
            sweep = await client.resolve_records(timeout=1.0)
        finally:
            await delivery
        assert client.last_sweep is sweep
        assert client.records[64].location == "Hall"
        assert client.capabilities[0xCB89] == {ModuleFunction.ROLLER: 1}
        assert client.cover_parameters[70].open_time_s == 40
        assert client.cover_parameters[70].calibration_percent == 10
        assert [e.sweep for e in events] == [sweep]
        assert 0xCB89 in sweep.answered_macs
    finally:
        await client.disconnect()


async def test_resolve_records_decodes_panel_settings_for_a_proven_board() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        # An M-DOT-4: a board whose params layout the library has proven.
        feed(
            client,
            ADMIN_DEVICES_TOPIC,
            devices(
                {
                    "id": 16,
                    "mac": 0xCB89,
                    "typ_urzadzenia": 8,
                    "wersja_pcb": 4,
                }
            ),
        )
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps({"List": []}),
                _list(
                    _device(
                        0xCB89,
                        0xCB89,
                        functions=caps((ModuleFunction.BACKLIGHT_RGBW, 4)),
                        params=panel_params(4),
                    )
                ),
            )
        )
        try:
            await client.resolve_records(timeout=1.0)
        finally:
            await delivery
        settings = client.panel_settings[0xCB89]
        # The field count came from the capability, not from a table here.
        assert len(settings.backlight_active) == 4
        assert settings.touch_field_color == (0, 0, 0, 255)
        assert settings.status_color == (255, 10, 10)
        assert settings.dim_after_s == 10
        assert settings.dim_brightness == 50
    finally:
        await client.disconnect()


async def test_resolve_records_leaves_an_unproven_board_out_of_panel_settings() -> None:

    client, broker = await _admin_client_with_catalogue()
    try:
        # Same panel family, a board revision nobody has read.
        feed(
            client,
            ADMIN_DEVICES_TOPIC,
            devices({"id": 16, "mac": 0xCB89, "typ_urzadzenia": 9, "wersja_pcb": 4}),
        )
        delivery = asyncio.create_task(
            _deliver_causally(
                client,
                broker,
                json.dumps({"List": []}),
                _list(
                    _device(
                        0xCB89,
                        0xCB89,
                        functions=caps((ModuleFunction.BACKLIGHT_RGBW, 18)),
                        params=panel_params(18),
                    )
                ),
            )
        )
        try:
            await client.resolve_records(timeout=1.0)
        finally:
            await delivery
        # The capability is there, so only the unproven layout stops it.
        assert client.capabilities[0xCB89] == {ModuleFunction.BACKLIGHT_RGBW: 18}
        assert 0xCB89 not in client.panel_settings
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
            ADMIN_PARAMS_DEVICES_TOPIC,
            params_of(
                {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"},
                {"id": 113, "typ_komponentu": "przekaznik", "leafId": "0_1_257_2_0"},
            ),
        )
        feed(
            client,
            ADMIN_DATA_DEVICES_TOPIC,
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
        assert client.module_records[1] == ModuleRecord()
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
        assert 64 not in client.records
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
        assert client.records == {}
        assert client.last_sweep is None
    finally:
        await client.disconnect()
