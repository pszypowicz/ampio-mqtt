"""Direct tests for the pure parsers in `ampio_mqtt._protocol`."""

from __future__ import annotations

import json

import pytest

from ampio_mqtt import (
    AccessTier,
    AmpioModule,
    AmpioProtocolError,
    AmpioServerInfo,
    BusEventRaised,
    ThermostatState,
)
from ampio_mqtt._protocol import (
    ENDPOINTS,
    RAW_BUZZER_OFF,
    RAW_BUZZER_SILENCE,
    RAW_IDENTIFY_OFF,
    RAW_IDENTIFY_ON,
    RAW_OUTPUT_FUNCTION_BY_SF,
    REDACTED,
    CatalogueDigest,
    DiagnosticsReport,
    EndpointReply,
    RawChannelEdge,
    Router,
    StateUpdate,
    md5_topic,
    parse_app_sync_devices,
    parse_details,
    parse_devices,
    parse_params_devices,
    parse_scenes,
    parse_server_info,
    parse_stan_json,
    parse_states_snapshot,
    raw_buzzer_pattern_payload,
    raw_buzzer_payload,
    raw_output_payload,
    raw_write_topic,
    redact_info_payload,
    server_below_baseline,
    to_int,
)

# One router per suite: topic classification is stateless per account.
# The full endpoint table: these tests cover topic shapes, not tier scoping.
_route = Router("u", ENDPOINTS).route


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        ("2", 2),
        (3.7, 3),
        (None, None),
        ("not-a-number", None),
        ("", None),
    ],
)
def test_to_int(value: object, expected: int | None) -> None:
    assert to_int(value) == expected


# Every column the live M-SERV serves on every row of one catalogue
# surface. A test that drops one is making a point about that column.
_ADMIN_COLUMNS = (
    "id",
    "id_urzadzenia",
    "typ_komponentu",
    "interpretacja",
    "funkcja",
    "leafId",
    "opis_menu",
    "type",
    "format",
    "params",
    "czas",
    "url",
)
_APP_SYNC_COLUMNS = (
    "id",
    "id_urzadzenia",
    "typ_komponentu",
    "interpretacja",
    "funkcja",
    "leafId",
    "opis_menu",
    "type",
    "format",
)


def _admin_row(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": 41,
        "id_urzadzenia": 3,
        "typ_komponentu": "temp",
        "interpretacja": 1,
        "funkcja": 7,
        "leafId": "0_cb8f_76_0_0",
        "opis_menu": "Salon",
        "type": None,
        "format": "",
        "params": 137438953473,  # 2**37 + 1: matter-exposed, not hidden
        "czas": 0,
        "url": "",
    }
    return {**row, **over}


def _app_row(**over: object) -> dict[str, object]:
    row = {k: v for k, v in _admin_row().items() if k in _APP_SYNC_COLUMNS}
    return {**row, **over}


def _rows(*items: dict[str, object]) -> str:
    return json.dumps({"Status": 0, "List": list(items)})


def test_parse_details_returns_metadata() -> None:
    items = parse_details(_rows(_admin_row()))
    assert [row.shared.id for row in items] == [41]
    shared = items[0].shared
    assert shared.id_urzadzenia == 3
    assert shared.typ_komponentu == "temp"
    assert shared.interpretacja == 1
    assert shared.funkcja == 7
    assert shared.leaf_id == "0_cb8f_76_0_0"
    assert shared.opis_menu == "Salon"
    assert shared.matter_device_type is None
    assert items[0].params == 137438953473
    assert items[0].czas == 0
    assert items[0].url == ""


def test_parse_app_sync_devices_returns_the_shared_columns() -> None:
    """The app-sync catalogue serves no `params`, `czas`, or `url` column.
    `data/params_devices` is that tier's source for the three."""
    items = parse_app_sync_devices(_rows(_app_row()))
    assert [row.id for row in items] == [41]
    assert items[0].typ_komponentu == "temp"
    assert items[0].funkcja == 7
    assert items[0].opis_menu == "Salon"


@pytest.mark.parametrize("column", _ADMIN_COLUMNS)
def test_parse_details_refuses_a_row_without_a_served_column(column: str) -> None:
    """Every listed column rides every live `devicesDetails` row, so a reply
    without one is a protocol break rather than an unconfigured object."""
    row = _admin_row()
    del row[column]
    with pytest.raises(AmpioProtocolError, match=column):
        parse_details(_rows(row))


@pytest.mark.parametrize("column", _APP_SYNC_COLUMNS)
def test_parse_app_sync_devices_refuses_a_row_without_a_served_column(
    column: str,
) -> None:
    row = _app_row()
    del row[column]
    with pytest.raises(AmpioProtocolError, match=column):
        parse_app_sync_devices(_rows(row))


@pytest.mark.parametrize(
    "column", ["id", "id_urzadzenia", "interpretacja", "funkcja", "params", "czas"]
)
def test_parse_details_refuses_a_column_that_is_not_an_integer(column: str) -> None:
    with pytest.raises(AmpioProtocolError, match=column):
        parse_details(_rows(_admin_row(**{column: "junk"})))


@pytest.mark.parametrize("column", ["typ_komponentu", "url"])
def test_parse_details_refuses_a_column_that_is_not_text(column: str) -> None:
    with pytest.raises(AmpioProtocolError, match=column):
        parse_details(_rows(_admin_row(**{column: 7})))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (17, 17),  # bit0 + bit4 (the live phantom shape)
        ("16", 16),  # string coerced
        (137438953473, 137438953473),  # >32-bit matter-exposed value
    ],
)
def test_parse_details_params(raw: object, expected: int) -> None:
    assert parse_details(_rows(_admin_row(params=raw)))[0].params == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("256", 256),  # 0x0100 On/Off Light, the tagged shape
        ("", None),  # untagged (config catalogue shape)
        (None, None),  # untagged (app-sync null shape)
    ],
)
def test_parse_details_matter_device_type(raw: object, expected: int | None) -> None:
    items = parse_details(_rows(_admin_row(type=raw)))
    assert items[0].shared.matter_device_type == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (500, 500),  # the raw 10 ms ticks
        ("500", 500),  # string coerced
        (0, 0),  # configured off
    ],
)
def test_parse_details_czas(raw: object, expected: int) -> None:
    assert parse_details(_rows(_admin_row(czas=raw)))[0].czas == expected


def _params_row(**over: object) -> dict[str, object]:
    """A well-formed `data/params_devices` row."""
    row: dict[str, object] = {"id": 5, "params": 17, "czas": 500, "url": "kWh"}
    return {**row, **over}


def test_parse_params_devices_carries_the_config_columns() -> None:
    table = parse_params_devices(_rows(_params_row()))
    assert table[5].params == 17
    assert table[5].czas == 500
    assert table[5].url == "kWh"


@pytest.mark.parametrize("column", ["id", "params", "czas", "url"])
def test_parse_params_devices_refuses_a_row_without_a_served_column(
    column: str,
) -> None:
    row = _params_row()
    del row[column]
    with pytest.raises(AmpioProtocolError, match=column):
        parse_params_devices(_rows(row))


def test_parse_params_devices_keeps_the_without_unit_sentinel() -> None:
    assert parse_params_devices(_rows(_params_row(url=" ")))[5].url == " "


@pytest.mark.parametrize(
    ("row", "url", "fmt"),
    [
        ({"url": "V", "format": "%.1f V"}, "V", "%.1f V"),
        ({"url": "", "format": "%.3f A"}, "", "%.3f A"),  # the live shape
        ({"url": " ", "format": ""}, " ", ""),  # "without unit" sentinel
        ({"url": "V", "format": None}, "V", ""),  # a null format reads as empty
    ],
)
def test_parse_details_url_and_format(row: dict, url: str, fmt: str) -> None:
    items = parse_details(_rows(_admin_row(**row)))
    assert items[0].url == url
    assert items[0].shared.format == fmt


@pytest.mark.parametrize(
    "parser",
    [
        parse_details,
        parse_app_sync_devices,
        parse_devices,
        parse_params_devices,
        parse_scenes,
        parse_states_snapshot,
        parse_server_info,
    ],
)
def test_unparseable_payloads_are_refused(parser) -> None:
    """A reply that is not the surface's own document shape is a protocol
    break. Nothing downstream can tell a tolerated one from an empty one."""
    with pytest.raises(AmpioProtocolError):
        parser("not json")


def _module_row(**over: object) -> dict[str, object]:
    """A well-formed `devices` module row."""
    row: dict[str, object] = {
        "id": 5,
        "mac": 0xCAFE,
        "mac_global": 0xBEEF,
        "nazwa_urzadzenia": "M-DOT-9",
        "typ_urzadzenia": 11,
        "wersja_softu": 908,
        "wersja_pcb": 12,
    }
    return {**row, **over}


def test_parse_devices_reads_the_module_row() -> None:
    modules = parse_devices(_rows(_module_row()))
    assert [m.id for m in modules] == [5]
    assert modules[0].mac == 0xCAFE and modules[0].mac_global == 0xBEEF
    assert modules[0].wersja_softu == 908 and modules[0].wersja_pcb == 12


@pytest.mark.parametrize("column", list(_module_row()))
def test_parse_devices_refuses_a_row_without_a_served_column(column: str) -> None:
    row = _module_row()
    del row[column]
    with pytest.raises(AmpioProtocolError, match=column):
        parse_devices(_rows(row))


def test_parse_states_snapshot_reads_every_row() -> None:
    entries = parse_states_snapshot(_rows({"id": 7, "stan_json": '{"state":"1"}'}))
    assert [(e.id, e.stan_json) for e in entries] == [(7, '{"state":"1"}')]


@pytest.mark.parametrize("column", ["id", "stan_json"])
def test_parse_states_snapshot_refuses_a_row_without_a_served_column(
    column: str,
) -> None:
    row = {"id": 7, "stan_json": '{"state":"1"}'}
    del row[column]
    with pytest.raises(AmpioProtocolError, match=column):
        parse_states_snapshot(_rows(row))


def test_state_route_non_dict_payload() -> None:
    """A JSON array payload falls through to text-mode and yields the raw string."""
    update = _route("ampio/fromDB/u/ob/41/state", json.dumps([1, 2]))
    assert isinstance(update, StateUpdate)
    assert update is not None
    assert update.state == "[1, 2]" and update.on_ms is None


def test_parse_devices_resolves_the_model_name() -> None:
    """The model column is derived from the type code. A type code outside
    the catalogue resolves to None rather than failing the row."""
    modules = parse_devices(
        _rows(
            _module_row(id=1, typ_urzadzenia=44),  # M-SENS
            _module_row(id=5, typ_urzadzenia=999),  # unknown type
        )
    )
    by_id = {m.id: m for m in modules}
    assert by_id[1].model == "M-SENS"
    assert by_id[5].model is None


def test_parse_devices_returns_modules() -> None:
    [module] = parse_devices(_rows(_module_row(id=3)))
    assert isinstance(module, AmpioModule)
    assert module.id == 3 and module.last_seen is None


def test_parse_server_info_extracts_safe_fields() -> None:
    payload = json.dumps(
        {
            "Results": {
                "mac": 1234,
                "userId": "-1",
                "serverVersion": "3.4.5",
                "local_ip": "192.168.1.10",
                "secretToken": "ignored",
            }
        }
    )
    info = parse_server_info(payload)
    assert info.mac == 1234
    assert info.user_id == -1
    assert info.server_version == "3.4.5"
    assert info.local_ip == "192.168.1.10"


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps([1, 2, 3]),
        # The baseline server always wraps the fields in `Results`.
        json.dumps({"mac": 1}),
        # ... and always reports its mac: an identity-less reply is refused,
        # which is what keeps `AmpioServerInfo.server_key` populated by
        # construction.
        json.dumps({"Results": {}}),
        json.dumps({"Results": {"serverVersion": "1865"}}),
    ],
)
def test_parse_server_info_refuses_a_reply_without_the_identity(payload: str) -> None:
    with pytest.raises(AmpioProtocolError):
        parse_server_info(payload)


def test_redact_info_payload_keeps_only_safelisted_values() -> None:
    """Every value outside the safe-key set is masked with the key kept,
    so the retained copy shows the reply's shape without the private data."""
    payload = json.dumps(
        {
            "Status": 0,
            "Results": {
                "mac": 1234,
                "userId": -1,
                "serverVersion": "1865",
                "serverRevision": "409",
                "mqttVersion": "5",
                "city": "Example Street 1, Springfield",
                "lat": "52.1000",
                "lon": "21.0000",
                "cloudInfo": {"host": "cloud.example", "port": 8883},
                "local_ip": "192.168.1.10",
                "device_id": "hw-0042",
                "publicKey": "PEMPEMPEM",
            },
        }
    )
    redacted = redact_info_payload(payload)
    data = json.loads(redacted)
    results = data["Results"]
    assert results["mac"] == 1234
    assert results["userId"] == -1
    assert results["serverVersion"] == "1865"
    assert results["serverRevision"] == "409"
    assert results["mqttVersion"] == "5"
    assert data["Status"] == 0
    private = ("city", "lat", "lon", "cloudInfo", "local_ip", "device_id", "publicKey")
    for key in private:
        assert results[key] == REDACTED
    assert "Springfield" not in redacted
    assert "52.1000" not in redacted


def test_redact_info_payload_masks_unknown_top_level_values() -> None:
    """A top-level key outside the safe set is masked too: the allowlist
    covers fields a future firmware adds anywhere in the envelope."""
    payload = json.dumps({"Results": {"mac": 1}, "debugDump": {"ip": "10.0.0.1"}})
    data = json.loads(redact_info_payload(payload))
    assert data["debugDump"] == REDACTED
    assert data["Results"] == {"mac": 1}


def test_redact_info_payload_withholds_unparseable_replies() -> None:
    """A reply without the parseable envelope is withheld outright: a
    truncated JSON string can carry the private fields in clear text."""
    for payload in (
        "not json",
        json.dumps([1, 2]),
        json.dumps({"mac": 1}),
        json.dumps({"Results": "text"}),
        '{"Results": {"city": "Example Str',
    ):
        assert redact_info_payload(payload) == REDACTED


def test_parse_server_info_coerces_numeric_version_fields() -> None:
    """The version fields are typed str; an int wire value must land as a
    string, or `server_below_baseline` would raise on splitting it."""
    payload = json.dumps(
        {"Results": {"mac": 1, "serverVersion": 1865, "serverRevision": 409}}
    )
    info = parse_server_info(payload)
    assert info is not None
    assert info.server_version == "1865"
    assert info.server_revision == "409"


@pytest.mark.parametrize(
    ("version", "below"),
    [
        ("1865", False),  # the recorded baseline itself
        ("1866", False),
        ("1865.1", False),
        ("1864", True),
        ("409", True),
        (None, True),
        ("", True),
        ("release-7", True),  # unparseable counts as below
    ],
)
def test_server_below_baseline(version: str | None, below: bool) -> None:
    assert server_below_baseline(version) is below


@pytest.mark.parametrize(
    ("user_id", "tier"),
    [
        (-1, AccessTier.ADMIN),
        (4, AccessTier.RESTRICTED),
        (0, AccessTier.RESTRICTED),
        (None, None),
    ],
)
def test_server_info_access_tier_from_account_id(
    user_id: int | None, tier: AccessTier | None
) -> None:
    assert AmpioServerInfo(mac=1, user_id=user_id).access_tier is tier


def test_state_route_json_payload() -> None:
    update = _route(
        "ampio/fromDB/u/ob/41/state", json.dumps({"state": "22.4", "on": 1700})
    )
    assert isinstance(update, StateUpdate)
    assert update.id == 41 and update.state == "22.4" and update.on_ms == 1700


def test_state_route_plain_payload() -> None:
    update = _route("ampio/fromDB/u/ob/41/state", "ok")
    assert isinstance(update, StateUpdate)
    assert update.state == "ok" and update.on_ms is None


def test_state_route_strips_plain_payload_whitespace() -> None:
    """A trailing newline must not flip `is_on`; the per-object plain form
    strips exactly as the raw channel form does."""
    update = _route("ampio/fromDB/u/ob/41/state", "0\n")
    assert isinstance(update, StateUpdate)
    assert update.state == "0"


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [
        pytest.param(24.4, "24.4", id="float"),
        pytest.param(1, "1", id="int"),
        pytest.param(0, "0", id="zero-int"),
        pytest.param(True, "True", id="bool"),
    ],
)
def test_state_route_coerces_numeric_state_to_str(
    raw_state: object, expected: str
) -> None:
    """Numeric JSON `state` values are normalized to text at the parser."""
    update = _route(
        "ampio/fromDB/u/ob/41/state",
        json.dumps({"state": raw_state, "on": 1700}),
    )
    assert isinstance(update, StateUpdate)
    assert update.state == expected
    assert isinstance(update.state, str)


def test_state_route_null_state_falls_back_to_payload() -> None:
    """An explicit `null` state preserves the raw payload as the value."""
    payload = json.dumps({"state": None, "on": 1700})
    update = _route("ampio/fromDB/u/ob/41/state", payload)
    assert isinstance(update, StateUpdate)
    assert update.state == payload


@pytest.mark.parametrize(
    "topic",
    [
        "ampio/fromDB/u/notob/41/state",
        "too/short",
        "ampio/fromDB/u/ob/notanint/state",
        "ampio/fromDB/u/ob/41/state/extra",
        "ampio/fromDB/otheruser/ob/41/state",
    ],
)
def test_state_route_invalid_topic(topic: str) -> None:
    assert _route(topic, "x") is None


def test_parse_stan_json_extracts_state_and_timestamp() -> None:
    seed = parse_stan_json(json.dumps({"state": "21.0", "on": 1700000000000}))
    assert seed is not None
    assert seed.state == "21.0" and seed.on_ms == 1700000000000


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [
        pytest.param(21.0, "21.0", id="float"),
        pytest.param(7, "7", id="int"),
    ],
)
def test_parse_stan_json_coerces_numeric_state_to_str(
    raw_state: object, expected: str
) -> None:
    """`stan_json` seeds normalize numeric state to text too."""
    seed = parse_stan_json(json.dumps({"state": raw_state, "on": 1}))
    assert seed is not None
    assert seed.state == expected
    assert isinstance(seed.state, str)


def test_parse_stan_json_null_state_yields_none() -> None:
    """An explicit `null` state preserves the None contract."""
    seed = parse_stan_json(json.dumps({"state": None, "on": 1}))
    assert seed is not None
    assert seed.state is None


@pytest.mark.parametrize("payload", ["", "not json", json.dumps([1, 2])])
def test_parse_stan_json_invalid(payload: str) -> None:
    assert parse_stan_json(payload) is None


# A reg state as a live M-SERV serializes it: every field a string, the
# spacing verbatim from the capture.
REG_PAYLOAD = (
    '{ "state": "0", "cooling": "0", "mode": "S",'
    '"measureTemp": "25.90","setTemperature": "21.00", "on": 1787682427583}'
)


def test_state_route_reg_payload_carries_thermostat() -> None:
    update = _route("ampio/fromDB/u/ob/138/state", REG_PAYLOAD)
    assert isinstance(update, StateUpdate)
    assert update.state == "0" and update.on_ms == 1787682427583
    assert update.thermostat == ThermostatState(
        measure_temp=25.9,
        set_temperature=21.0,
        mode="S",
        cooling=False,
    )


def test_state_route_plain_shape_has_no_thermostat() -> None:
    update = _route(
        "ampio/fromDB/u/ob/41/state", json.dumps({"state": "1", "desc": "x", "on": 1})
    )
    assert isinstance(update, StateUpdate)
    assert update.thermostat is None


def test_parse_stan_json_reg_shape_carries_thermostat() -> None:
    seed = parse_stan_json(REG_PAYLOAD)
    assert seed is not None
    assert seed.thermostat == ThermostatState(
        measure_temp=25.9,
        set_temperature=21.0,
        mode="S",
        cooling=False,
    )


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(
            {"mode": "A"},
            ThermostatState(
                measure_temp=None,
                set_temperature=None,
                mode="A",
                cooling=None,
            ),
            id="mode-only",
        ),
        pytest.param(
            {"cooling": "1", "measureTemp": "junk", "setTemperature": "inf"},
            ThermostatState(
                measure_temp=None,
                set_temperature=None,
                mode=None,
                cooling=True,
            ),
            id="cooling-true-unparseable-temps",
        ),
    ],
)
def test_reg_shape_partial_fields(fields: dict, expected: ThermostatState) -> None:
    """Any reg key makes the shape; absent or unparseable fields read None."""
    update = _route("ampio/fromDB/u/ob/138/state", json.dumps({"state": "0", **fields}))
    assert isinstance(update, StateUpdate)
    assert update.thermostat == expected


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("ampio/from/CFFE/state/f/32", (0xCFFE, "f", 32)),
        ("ampio/from/1/state/i/3", (1, "i", 3)),
        # MAC is parsed as hex int, so case/zero-padding is normalized.
        ("ampio/from/00cffe/state/f/1", (0xCFFE, "f", 1)),
    ],
)
def test_raw_channel_route_ok(topic: str, expected: tuple[int, str, int]) -> None:
    edge = _route(topic, " 1 ")
    assert isinstance(edge, RawChannelEdge)
    assert (edge.mac, edge.prefix, edge.channel) == expected
    assert edge.state == "1"  # payload arrives stripped


@pytest.mark.parametrize(
    "topic",
    [
        "ampio/from/CFFE/state/f",  # too short
        "ampio/from/CFFE/state/f/32/extra",  # too long
        "ampio/from/CFFE/notstate/f/32",  # wrong segment
        "ampio/to/CFFE/state/f/32",  # not a 'from' topic
        "ampio/from/ZZZZ/state/f/32",  # non-hex mac
        "ampio/from/CFFE/state/f/notint",  # non-int channel
    ],
)
def test_raw_channel_route_malformed(topic: str) -> None:
    assert _route(topic, "1") is None


@pytest.mark.parametrize(
    ("topic", "payload", "expected"),
    [
        ("ampio/from/1/event", "189", BusEventRaised(event_number=189, mac=1)),
        ("ampio/from/D09A/event", " 42 ", BusEventRaised(event_number=42, mac=0xD09A)),
    ],
)
def test_event_route_ok(topic: str, payload: str, expected: BusEventRaised) -> None:
    assert _route(topic, payload) == expected


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("ampio/from/zz/event", "189"),  # non-hex mac
        ("ampio/from/1/event", "not-a-number"),
        ("ampio/from/1/2/event", "189"),  # wrong depth
        ("ampio/to/1/event", "189"),  # not a 'from' topic
        ("nmpio/from/1/event", "189"),  # wrong root
    ],
)
def test_event_route_malformed(topic: str, payload: str) -> None:
    assert _route(topic, payload) is None


def test_diagnostics_route_ok() -> None:
    report = _route("ampio/from/cafe/b/4F", json.dumps({"d": [254, 79, 63, 142]}))
    assert isinstance(report, DiagnosticsReport)
    assert report.mac == 0xCAFE
    assert report.diagnostics.supply_voltage == 12.6
    assert report.diagnostics.temperature == 42.0


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("ampio/from/cafe/b/50", '{"d": [254, 79, 63, 142]}'),  # not the 4F frame
        ("ampio/from/zz/b/4F", '{"d": [254, 79, 63, 142]}'),  # non-hex mac
        ("ampio/from/cafe/b/4F", "not json"),  # unparseable frame
        ("ampio/from/cafe/b/4F/extra", '{"d": [254, 79, 63, 142]}'),
        ("ampio/from/cafe/b/4F", '{"d": [254, 80, 63, 142]}'),  # wrong frame type
        ("ampio/from/cafe/b/4F", '{"d": [1, 79, 63, 142]}'),  # not a broadcast
        ("ampio/from/cafe/b/4F", '{"d": [254, 79]}'),  # truncated
    ],
)
def test_diagnostics_route_malformed(topic: str, payload: str) -> None:
    assert _route(topic, payload) is None


def test_endpoint_reply_route_carries_raw_payload() -> None:
    reply = _route("ampio/fromDB/u/config/devicesDetails", "{corrupt")
    assert isinstance(reply, EndpointReply)
    assert reply.endpoint.name == "details"
    assert reply.payload == "{corrupt"  # unparsed: the store's handlers decide


def test_route_is_user_scoped_for_endpoint_replies() -> None:
    assert _route("ampio/fromDB/other/config/devicesDetails", "{}") is None


@pytest.mark.parametrize("keyword", ["devices", "params_devices"])
def test_digest_route_ok(keyword: str) -> None:
    digest = _route(md5_topic("u", keyword), "0f343b0931126a20f133d67c2b018a3b\n")
    assert digest == CatalogueDigest(
        keyword=keyword, digest="0f343b0931126a20f133d67c2b018a3b"
    )


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        ("ampio/fromDB/u/md5/scenes", "abc"),  # no catalogue behind it
        ("ampio/fromDB/other/md5/devices", "abc"),  # another account
        ("ampio/fromDB/u/md5/devices", ""),  # a retained clear, not a digest
        ("ampio/fromDB/u/md5", "abc"),
    ],
)
def test_digest_route_rejects(topic: str, payload: str) -> None:
    assert _route(topic, payload) is None


def test_diagnostics_three_element_frame_has_no_temperature() -> None:
    report = _route("ampio/from/cafe/b/4F", '{"d": [254, 79, 61]}')
    assert isinstance(report, DiagnosticsReport)
    assert report.diagnostics.supply_voltage == 12.2
    assert report.diagnostics.temperature is None


# --- raw write builders ---------------------------------------------------


def test_raw_write_topic_is_lowercase_hex() -> None:
    assert raw_write_topic(0xCAFE) == "ampio/to/cafe/raw"
    assert raw_write_topic(1) == "ampio/to/1/raw"


def test_raw_output_payload_encodes_function_value_and_channel() -> None:
    assert raw_output_payload(0x30, 255, 1) == "30f9ff01"
    assert raw_output_payload(0x30, 0, 0) == "30f90000"
    assert raw_output_payload(0x32, 128, 23) == "32f98017"


def test_raw_output_function_is_mapped_per_proven_leaf_class() -> None:
    """Binary outputs (257) take 0x30, open-collector outputs (67) take 0x32;
    no other class has a proven byte."""
    assert RAW_OUTPUT_FUNCTION_BY_SF == {257: 0x30, 67: 0x32}


def test_raw_buzzer_payload_encodes_the_simple_action() -> None:
    """Sub-function ON or OFF, tone, and the time byte in 10 ms ticks."""
    assert raw_buzzer_payload(True, 6, 50) == "0c070370010632"
    assert raw_buzzer_payload(False, 6, 0) == "0c070370000600"


def test_raw_buzzer_pattern_payload_encodes_the_sequence_action() -> None:
    """Two tones with 16-bit little-endian times, a cycle count, and a delay."""
    assert raw_buzzer_pattern_payload(6, 30, 20, 30, 3, 0) == (
        "0c07037101000006001e0014001e0003"
    )
    assert raw_buzzer_pattern_payload(6, 30, 6, 0, 1, 100) == (
        "0c07037101640006001e000600000001"
    )
    assert raw_buzzer_pattern_payload(6, 400, 6, 0, 1, 0) == (
        "0c070371010000060090010600000001"
    )


def test_raw_buzzer_stop_frames() -> None:
    assert RAW_BUZZER_SILENCE == "0c070371010000000001000000000001"
    assert RAW_BUZZER_OFF == "0c070370000600"


def test_raw_identify_frames_are_the_designer_pair() -> None:
    """`[0x7E, flag]` as ASCII hex: 1 starts identify, 0 stops it."""
    assert RAW_IDENTIFY_ON == "7e01"
    assert RAW_IDENTIFY_OFF == "7e00"
