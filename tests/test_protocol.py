"""Direct tests for the pure parsers in `ampio_mqtt._protocol`."""

from __future__ import annotations

import json

import pytest

from ampio_mqtt import (
    AccessTier,
    AmpioModule,
    AmpioServerInfo,
    BusEvent,
    ThermostatState,
)
from ampio_mqtt._protocol import (
    ENDPOINTS,
    REDACTED,
    DiagnosticsReport,
    EndpointReply,
    RawChannelEdge,
    Router,
    StateUpdate,
    parse_details,
    parse_devices,
    parse_params_devices,
    parse_scenes,
    parse_server_info,
    parse_stan_json,
    parse_states_snapshot,
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


def test_parse_details_returns_metadata() -> None:
    payload = json.dumps(
        {
            "List": [
                {
                    "id": 41,
                    "id_urzadzenia": 3,
                    "typ_komponentu": "temp",
                    "interpretacja": 1,
                    "funkcja": 7,
                    "leafId": "0_cb8f_76_0_0",
                    "params": 137438953473,  # 2**37 + 1: matter-exposed, not hidden
                    "opis_menu": "Salon",
                    "stan_json": json.dumps({"state": "21.5", "on": 1700000000000}),
                },
                {"id": "bad"},  # skipped: non-int id
                {"id": 42},  # kept: minimal record
            ]
        }
    )
    items = parse_details(payload)
    assert items is not None
    assert [m.id for m in items] == [41, 42]
    assert items[0].opis_menu == "Salon"
    assert items[0].funkcja == 7
    assert items[0].leaf_id == "0_cb8f_76_0_0"
    assert items[0].stan_json is not None
    assert items[1].opis_menu is None and items[1].stan_json is None
    assert items[1].funkcja is None  # absent -> None
    assert items[1].leaf_id == ""  # absent -> empty string
    assert items[1].matter_device_type is None  # absent -> None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (17, 17),  # bit0 + bit4 (the live phantom shape)
        ("16", 16),  # string coerced
        (137438953473, 137438953473),  # >32-bit matter-exposed value
        (None, None),  # absent column -> None, so the client keeps what it has
        ("not-a-number", None),  # junk is indistinguishable from absent
    ],
)
def test_parse_details_params(raw: object, expected: int | None) -> None:
    payload = json.dumps({"List": [{"id": 1, "params": raw}]})
    items = parse_details(payload)
    assert items is not None and items[0].params == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("256", 256),  # 0x0100 On/Off Light, the tagged shape
        ("", None),  # untagged (config catalogue shape)
        (None, None),  # untagged (app-sync null shape)
    ],
)
def test_parse_details_matter_device_type(raw: object, expected: int | None) -> None:
    payload = json.dumps({"List": [{"id": 1, "type": raw}]})
    items = parse_details(payload)
    assert items is not None and items[0].matter_device_type == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"id": 1, "czas": 500}, 5000),  # 10 ms wire ticks -> ms
        ({"id": 1, "czas": "500"}, 5000),  # string coerced
        ({"id": 1, "czas": 0}, 0),  # configured off
        ({"id": 1}, None),  # absent column -> None, the client keeps what it has
        ({"id": 1, "czas": "junk"}, None),  # junk is indistinguishable from absent
    ],
)
def test_parse_details_pulse_ms(row: dict, expected: int | None) -> None:
    items = parse_details(json.dumps({"List": [row]}))
    assert items is not None and items[0].pulse_ms == expected


@pytest.mark.parametrize(
    ("row", "params", "pulse_ms"),
    [
        ({"id": 5, "params": 17, "czas": 500}, 17, 5000),
        ({"id": 5, "params": 17}, 17, 0),  # the table is complete: absent = off
        ({"id": 5, "czas": 500}, 0, 5000),
    ],
)
def test_parse_params_devices_carries_pulse_ms(
    row: dict, params: int, pulse_ms: int
) -> None:
    table = parse_params_devices(json.dumps({"List": [row]}))
    assert table is not None
    assert table[5].params == params
    assert table[5].pulse_ms == pulse_ms


@pytest.mark.parametrize(
    "parser",
    [
        parse_details,
        parse_devices,
        parse_params_devices,
        parse_scenes,
        parse_states_snapshot,
    ],
)
def test_unparseable_payloads_return_none(parser) -> None:
    assert parser("not json") is None


@pytest.mark.parametrize(
    ("parser", "rows", "surviving_ids"),
    [
        (parse_details, [{"id": "x"}, {"id": 5}], lambda r: [i.id for i in r]),
        (
            parse_devices,
            [{"id": "x"}, {"id": 5, "typ_urzadzenia": 1}],
            lambda r: [m.id for m in r],
        ),
        (
            parse_params_devices,
            [{"id": "x", "params": 1}, {"id": 5, "params": 17}],
            lambda r: list(r),
        ),
        (
            parse_scenes,
            [{"id": None, "sceneName": "Bad"}, {"id": 5, "sceneName": "Good"}],
            lambda r: [s.id for s in r],
        ),
        (parse_states_snapshot, [{"id": "x"}, {"id": 5}], lambda r: [e.id for e in r]),
    ],
)
def test_rows_without_an_int_id_are_skipped(parser, rows, surviving_ids) -> None:
    result = parser(json.dumps({"List": rows}))
    assert result is not None and surviving_ids(result) == [5]


def test_state_route_non_dict_payload() -> None:
    """A JSON array payload falls through to text-mode and yields the raw string."""
    update = _route("ampio/fromDB/u/ob/41/state", json.dumps([1, 2]))
    assert isinstance(update, StateUpdate)
    assert update is not None
    assert update.value == "[1, 2]" and update.on_ms is None


def test_parse_devices_resolves_the_model_name() -> None:
    """The model column is derived from the type code; unknown or missing
    types resolve to None rather than failing the row."""
    payload = json.dumps(
        {
            "List": [
                {"id": 1, "typ_urzadzenia": 44},  # M-SENS
                {"id": 5, "typ_urzadzenia": 999},  # unknown type
                {"id": 6},  # no typ_urzadzenia
            ]
        }
    )
    modules = parse_devices(payload)
    assert modules is not None
    by_id = {m.id: m for m in modules}
    assert by_id[1].model == "M-SENS"
    assert by_id[5].model is None
    assert by_id[6].model is None


def test_parse_devices_returns_modules() -> None:
    payload = json.dumps(
        {
            "List": [
                {
                    "id": 3,
                    "mac": 10,
                    "mac_global": 1234,
                    "nazwa_urzadzenia": "M-SENS",
                    "typ_urzadzenia": 5,
                    "wersja_softu": 7,
                    "wersja_pcb": 1,
                },
            ]
        }
    )
    modules = parse_devices(payload)
    assert modules is not None
    [module] = modules
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
    assert info is not None
    assert info.mac == 1234
    assert info.user_id == -1
    assert info.server_version == "3.4.5"
    assert info.local_ip == "192.168.1.10"


def test_parse_server_info_bad_payload_returns_none() -> None:
    assert parse_server_info("not json") is None
    assert parse_server_info(json.dumps([1, 2, 3])) is None
    # The baseline server always wraps the fields in `Results`.
    assert parse_server_info(json.dumps({"mac": 1})) is None
    # ... and always reports its mac: an identity-less reply is unparseable,
    # which is what keeps `AmpioServerInfo.key` populated by construction.
    assert parse_server_info(json.dumps({"Results": {}})) is None
    assert parse_server_info(json.dumps({"Results": {"serverVersion": "1865"}})) is None


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
    assert update.id == 41 and update.value == "22.4" and update.on_ms == 1700


def test_state_route_plain_payload() -> None:
    update = _route("ampio/fromDB/u/ob/41/state", "ok")
    assert isinstance(update, StateUpdate)
    assert update.value == "ok" and update.on_ms is None


def test_state_route_strips_plain_payload_whitespace() -> None:
    """A trailing newline must not flip `is_on`; the per-object plain form
    strips exactly as the raw channel form does."""
    update = _route("ampio/fromDB/u/ob/41/state", "0\n")
    assert isinstance(update, StateUpdate)
    assert update.value == "0"


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
    assert update.value == expected
    assert isinstance(update.value, str)


def test_state_route_null_state_falls_back_to_payload() -> None:
    """An explicit `null` state preserves the raw payload as the value."""
    payload = json.dumps({"state": None, "on": 1700})
    update = _route("ampio/fromDB/u/ob/41/state", payload)
    assert isinstance(update, StateUpdate)
    assert update.value == payload


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


def test_parse_stan_json_extracts_value_and_timestamp() -> None:
    seed = parse_stan_json(json.dumps({"state": "21.0", "on": 1700000000000}))
    assert seed is not None
    assert seed.value == "21.0" and seed.on_ms == 1700000000000


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
    assert seed.value == expected
    assert isinstance(seed.value, str)


def test_parse_stan_json_null_state_yields_none() -> None:
    """An explicit `null` state preserves the None contract."""
    seed = parse_stan_json(json.dumps({"state": None, "on": 1}))
    assert seed is not None
    assert seed.value is None


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
    assert update.value == "0" and update.on_ms == 1787682427583
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
    assert edge.value == "1"  # payload arrives stripped


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
        ("ampio/from/1/event", "189", BusEvent(number=189, mac=1)),
        ("ampio/from/D09A/event", " 42 ", BusEvent(number=42, mac=0xD09A)),
    ],
)
def test_event_route_ok(topic: str, payload: str, expected: BusEvent) -> None:
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


def test_diagnostics_three_element_frame_has_no_temperature() -> None:
    report = _route("ampio/from/cafe/b/4F", '{"d": [254, 79, 61]}')
    assert isinstance(report, DiagnosticsReport)
    assert report.diagnostics.supply_voltage == 12.2
    assert report.diagnostics.temperature is None


# --- raw write builders ---------------------------------------------------


def test_raw_write_topic_is_lowercase_hex() -> None:
    assert raw_write_topic(0xCAFE) == "ampio/to/cafe/raw"
    assert raw_write_topic(1) == "ampio/to/1/raw"


def test_raw_output_payload_encodes_value_and_channel() -> None:
    assert raw_output_payload(255, 1) == "30f9ff01"
    assert raw_output_payload(0, 0) == "30f90000"
    assert raw_output_payload(128, 23) == "30f98017"
