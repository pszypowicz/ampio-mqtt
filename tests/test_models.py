"""Tests for the pure AmpioObject model properties - no client, no broker."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ampio_mqtt import AmpioModule, AmpioObject, AmpioServerInfo, ModuleAddress
from ampio_mqtt.classification import InputKind, ThermostatKind, classify
from ampio_mqtt.device_types import module_model
from ampio_mqtt.models import DesignerRecord, ModuleRecord


# Every catalogue row carries these columns on both tiers, so an object
# always holds them. A test names only what it is about.
def _object(**over: object) -> AmpioObject:
    row: dict[str, object] = {
        "id": 1,
        "typ_komponentu": "",
        "interpretacja": 0,
        "funkcja": 1,
        "address": ModuleAddress(mac=0xCAFE, channel=0, sf_id=257, sub_sf_id=0),
        "leaf_key": "leaf_0_cafe_257_0_0",
    }
    return AmpioObject(**{**row, **over})  # type: ignore[arg-type]


def _module(**over: object) -> AmpioModule:
    """A module carrying the columns every module-list row serves."""
    row: dict[str, object] = {
        "id": 1,
        "mac": 0xCAFE,
        "mac_global": 0xBEEF,
        "typ_urzadzenia": 44,
        "wersja_softu": 908,
        "wersja_pcb": 1,
    }
    return AmpioModule(**{**row, **over})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("1", True), ("255", True)],
)
def test_is_on_interpretation(value, expected) -> None:
    assert _object(id=1, state=value).is_on is expected


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
    assert _object(id=1, state=value).numeric_value == expected


def test_object_key_is_the_per_object_token() -> None:
    """object_key returns obj_<id>."""
    obj = _object(id=7)
    assert obj.object_key == "obj_7"


def test_object_key_is_the_object_id() -> None:
    assert _object(id=150, leaf_id="0_be82_257_2_2").object_key == "obj_150"


def test_object_key_separates_views_of_one_output() -> None:
    """Two Designer views of one output share a leaf but not an identity."""
    leaf = "0_be82_257_2_2"
    relay_view = _object(id=150, leaf_id=leaf, leaf_key=f"leaf_{leaf}")
    bell_view = _object(id=151, leaf_id=leaf, leaf_key=f"leaf_{leaf}")
    assert relay_view.leaf_key == bell_view.leaf_key == f"leaf_{leaf}"
    assert relay_view.object_key != bell_view.object_key


def test_object_key_survives_an_empty_leaf_id() -> None:
    """System objects and Matter-unchecked rows carry no leaf, but do carry an id."""
    assert _object(id=99, leaf_id="").object_key == "obj_99"


@pytest.mark.parametrize(
    ("typ", "leaf_id", "params", "visible"),
    [
        # Real object with a non-empty leafId (the real-install shape).
        ("temp", "0_cb8f_76_0_0", 0, True),
        # A relay whose Matter box was unchecked: Designer clears leafId and
        # the row keeps its type, its module, and its state.
        ("przekaznik", "", 0, True),
        # The DELETED bit hides a row whatever its leafId says.
        ("temp", "0_cb8f_76_0_0", 16, False),
        ("przekaznik", "", 16, False),
        # A missing typ_komponentu reads like any other row.
        (None, "", 0, True),
        (None, "0_x_x_x_x", 16, False),
    ],
)
def test_visibility_predicate(
    typ: str | None,
    leaf_id: str,
    params: int,
    visible: bool,
) -> None:
    obj = _object(id=1, typ_komponentu=typ, leaf_id=leaf_id, params=params)
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
    obj = _object(id=1, params=params)
    assert obj.hidden is hidden


@pytest.mark.parametrize(
    ("params", "read_only"),
    [
        (0, False),  # absent -> writable
        (1, False),  # the live writable-flag shape
        (64, True),  # bit 6 -> Designer read-only checkbox
        (65, True),  # bit 0 + bit 6 (the live read-only-flag shape)
        (16, False),  # hidden is not a writability signal
        ((1 << 37) | 64, True),  # Matter opt-in does not clear it
    ],
)
def test_read_only_reads_params_bit_6(params: int, read_only: bool) -> None:
    obj = _object(id=1, params=params)
    assert obj.read_only is read_only


@pytest.mark.parametrize(
    ("typ", "czas", "pulse_ms"),
    [
        ("przekaznik", 500, 5000),  # 10 ms ticks -> ms, the live bell-relay value
        ("flaga", 500, 5000),  # the timed form reverts a flag
        ("led", 50, 500),  # and a dimmer, with no intermediate level
        ("przekaznik", 0, 0),
        # Designer offers the field, and the timed write latches instead of
        # reverting, so there is no pulse length to report.
        ("flaga_liniowa", 500, 0),
        ("flaga_liniowa16", 500, 0),
        ("ledww", 500, 0),  # the timed form zeroes the coldness and holds
        # No TYPE_PROFILES row, so no claim either way about the wire.
        ("rgb", 500, 0),
        ("rgbww", 500, 0),
        ("rgbw", 500, 0),  # not on Designer's list: no turn-on time field
        ("roleta_procenty", 500, 0),  # covers never get the field
        ("kamera", 500, 0),  # the same column is a refresh time in ms there
        (None, 500, 0),
    ],
)
def test_pulse_ms_reads_czas_only_where_a_timed_write_pulses(
    typ: str | None, czas: int, pulse_ms: int
) -> None:
    assert _object(id=1, typ_komponentu=typ, czas=czas).pulse_ms == pulse_ms


@pytest.mark.parametrize(
    ("typ", "url", "fmt", "unit"),
    [
        ("bit32", "", "%.3f A", "A"),  # the live meter: unit typed into the format
        ("bit32", "V", "%.1f V", "V"),  # the dropdown-composed shape
        ("bit32", "V", "%.3f", "V"),  # a format without a tail leaves the url
        ("bit32", "V", "%.3f A", "A"),  # Designer: the format overwrites the unit
        ("bit32", " ", "%.1f", None),  # the "without unit" sentinel
        ("bit32", " ", "", None),
        ("bit32", "%", "", "%"),  # Designer writes % when the box is unticked
        ("bit32", "", "%.1f %%", "%"),  # the printf escape is one literal percent
        ("bit32", "", "%d%%", "%"),
        ("bit32", "", "abc", None),  # no conversion, no tail
        ("bit32", "V", "abc", "V"),
        ("bit32", "  A  ", "", "A"),  # stripped
        ("lin_wej", "IAQ", "", "IAQ"),  # the live air-quality row
        ("temp", "°C", "", "°C"),
        ("no_such_type", "kWh", "", "kWh"),  # unknown types are the generic sensor
        ("przekaznik", "V", "%.1f V", None),  # a unit applies to measurements only
        ("reg", "°C", "", None),
    ],
)
def test_unit_reads_the_format_tail_then_the_url_on_sensor_kinds(
    typ: str, url: str, fmt: str, unit: str | None
) -> None:
    obj = _object(id=1, typ_komponentu=typ, interpretacja=1, url=url, format=fmt)
    assert obj.unit == unit


@pytest.mark.parametrize(
    ("typ", "fmt", "decimals"),
    [
        ("bit32", "%.3f A", 3),  # the live meter shape
        ("bit32", "%.1f", 1),  # the Designer dropdown, in its order
        ("bit32", "%.2f", 2),
        ("bit32", "%.0f", 0),
        ("bit32", "%6.2f", 2),
        ("bit32", "%06.2f", 2),
        ("bit32", "%+6.2f", 2),
        ("bit32", "%.3e", None),  # scientific notation has no fixed precision
        ("bit32", "%g", None),
        ("bit32", "%.3g", None),
        ("bit32", "%#x", None),
        ("bit32", "%.2F", 2),
        ("bit32", "%.1f %%", 1),
        ("bit32", "%f", None),  # only an explicit precision counts
        ("bit32", "%d", None),
        ("bit32", "", None),
        ("bit32", "abc", None),
        ("przekaznik", "%.1f", None),  # measurements only, like `unit`
    ],
)
def test_decimals_reads_the_explicit_precision_of_a_fixed_point_format(
    typ: str, fmt: str, decimals: int | None
) -> None:
    obj = _object(id=1, typ_komponentu=typ, interpretacja=1, format=fmt)
    assert obj.decimals == decimals


@pytest.mark.parametrize(
    ("typ", "params", "bell"),
    [
        ("przekaznik", 1 << 15, True),  # bit 15 -> Designer bell-object checkbox
        ("przekaznik", 134250497, True),  # the live bell-relay shape (bits 0/15/27)
        ("flaga", 1 << 15, True),  # the checkbox exists on flags too
        ("przekaznik", 1, False),  # the live plain-relay shape
        ("przekaznik", 0, False),  # absent -> not a bell
        ("led", 1 << 15, False),  # OPTION1 on a dimmer = show-switch-in-slider
        ("roleta_lamelki", 1 << 15, False),  # OPTION1 on a tilt cover = 1% lamella
        (None, 1 << 15, False),  # unknown type -> the bit's meaning is unknown
    ],
)
def test_bell_reads_params_bit_15_only_on_relay_and_flag(
    typ: str | None, params: int, bell: bool
) -> None:
    obj = _object(id=1, typ_komponentu=typ, params=params)
    assert obj.bell is bell


def test_hidden_overrides_leaf_id_visibility() -> None:
    """Bit 4 (hidden) drops an object even when its leaf_id would show it.

    This is the duplicated-Designer-channel case: a phantom and its labelled
    twin share a leaf_id, so the leaf_id heuristic keeps both and the consumer's
    unique-id collides. The phantom carries bit 4, so it is filtered out.
    """
    phantom = _object(
        id=1, typ_komponentu="lin_wej", leaf_id="0_cb97_74_0_1", params=17
    )
    labelled = _object(
        id=2, typ_komponentu="lin_wej", leaf_id="0_cb97_74_0_1", params=(1 << 37) | 1
    )
    assert phantom.visible is False
    assert labelled.visible is True


# --- derived fields: kind and model own their inputs (#94) ------------------


def test_kind_derives_from_the_metadata_inputs() -> None:
    """A seeded instance carries the same kind the store would compute -
    the derivation lives in the model, not at every construction site."""
    assert _object(id=1, typ_komponentu="led").kind == classify("led", None)
    assert _object(id=1).kind == classify(None, None)  # the generic sensor


def test_kind_rederives_on_replace() -> None:
    obj = _object(id=1, typ_komponentu="led")
    assert replace(obj, typ_komponentu="rgbw").kind == classify("rgbw", None)


def test_kind_cannot_be_passed() -> None:
    """No instance can hold a kind that disagrees with its inputs."""
    with pytest.raises(TypeError):
        _object(id=1, kind=classify("led", None))  # type: ignore[call-arg]


def test_module_model_derives_from_type() -> None:
    module = _module(id=1, typ_urzadzenia=4)
    assert module.model == module_model(4)
    assert module.model is not None
    assert replace(module, typ_urzadzenia=None).model is None
    with pytest.raises(TypeError):
        _module(id=1, model="M-REL")  # type: ignore[call-arg]


def test_reg_classifies_as_thermostat_and_surfaces_the_running_flag() -> None:
    obj = _object(id=138, typ_komponentu="reg")
    assert isinstance(obj.kind, ThermostatKind)
    assert replace(obj, state="1").is_on  # the surfaced value is the running flag


@pytest.mark.parametrize(
    ("leaf_id", "expected"),
    [
        ("0_cb8f_76_0_0", 0xCB8F),
        ("0_1_10_0_0", 1),  # the M-SERV's override mac, not its factory id
        ("0_D09A_5_1_2", 0xD09A),  # uppercase hex parses too
        ("", None),  # an empty leafId: Matter box unchecked
        ("0_cb8f_76_0", None),  # four segments
        ("0_cb8f_76_0_0_9", None),  # six segments
        ("1_cb8f_76_0_0", None),  # unexpected leading segment
        ("0_zz_76_0_0", None),  # non-hex mac segment
        ("0__76_0_0", None),  # empty mac segment
    ],
)
def test_module_mac_parses_strictly(leaf_id: str, expected: int | None) -> None:
    """`0_<macHex>_<sfId>_<subSfId>_<ioNo>` yields the module's override mac;
    any other shape yields None rather than a half-parsed guess."""
    assert _object(id=1, leaf_id=leaf_id).module_mac == expected


@pytest.mark.parametrize(
    ("leaf_id", "server_owned"),
    [
        ("0_1_10_0_0", True),  # the M-SERV's override mac
        ("0_cb8f_76_0_0", False),  # another module's object
        ("", False),  # an empty leafId
    ],
)
def test_is_server_owned_reads_the_mserv_override_mac(
    leaf_id: str, server_owned: bool
) -> None:
    """Served identically on both tiers via leafId, so server-owned
    objects anchor to the hub device without a module catalogue."""
    assert _object(id=1, leaf_id=leaf_id).is_server_owned is server_owned


@pytest.mark.parametrize(("mac", "expected"), [(47846, "47846"), (1, "1")])
def test_server_key_is_the_decimal_mac(mac: int, expected: str) -> None:
    """The canonical registry-scoping string; its format is a promise."""
    assert AmpioServerInfo(mac=mac, user_id=-1).server_key == expected


def _colored(value: str | None) -> AmpioObject:
    return _object(id=1, typ_komponentu="rgbw", state=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2214599934", (254, 28, 0, 132)),
        # The same word in signed 32-bit form, as the Matter bridge emits.
        ("-2080367362", (254, 28, 0, 132)),
        ("657930", (10, 10, 10, 0)),
        ("0", (0, 0, 0, 0)),
        ("4294967295", (255, 255, 255, 255)),
        ("4294967296", None),  # past 32 bits
        ("-2147483648", (0, 0, 0, 128)),  # INT32_MIN, the deepest signed form
        ("-2147483649", None),  # below the signed window: no 32-bit encoding
        ("-4294967296", None),
        ("junk", None),
        (None, None),
    ],
)
def test_rgbw_decodes_the_packed_state(
    value: str | None, expected: tuple[int, int, int, int] | None
) -> None:
    assert _colored(value).rgbw == expected


def test_rgbw_reads_none_for_non_color_kinds() -> None:
    """A dimmer's 0-255 level must not masquerade as a color."""
    dimmer = _object(id=1, typ_komponentu="led", state="255")
    assert dimmer.rgbw is None


def _cct(value: str | None) -> AmpioObject:
    return _object(id=1, typ_komponentu="ledww", state=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", (0, 0)),
        ("21844", (84, 85)),  # 0x5554
        ("65535", (255, 255)),
        ("255", (255, 0)),  # power only, coldness at the cold end of 0
        ("65280", (0, 255)),  # coldness only, power off
        ("65536", None),  # past 16 bits
        ("-1", None),
        ("junk", None),
        (None, None),
    ],
)
def test_cct_decodes_the_packed_state(
    value: str | None, expected: tuple[int, int] | None
) -> None:
    """`power | coldness<<8`, the form both the per-object topic and the
    module's own broadcast carry."""
    assert _cct(value).cct == expected


def test_cct_reads_none_for_non_color_temp_kinds() -> None:
    """A dimmer's level and an RGBW light's packed color must not
    masquerade as a color temperature."""
    assert _object(id=1, typ_komponentu="led", state="255").cct is None
    assert _colored("16777215").cct is None


def _cover(value: str | None, typ: str = "roleta_procenty") -> AmpioObject:
    return _object(id=1, typ_komponentu=typ, state=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", 0), ("55", 55), ("100", 100), ("101", None), ("junk", None), (None, None)],
)
def test_position_reads_the_travel_percent(
    value: str | None, expected: int | None
) -> None:
    assert _cover(value).position == expected


def test_position_reads_none_off_the_position_axis() -> None:
    """A plain up/down cover has no position axis; neither has a light."""
    assert _cover("55", typ="roleta").position is None
    assert _colored("55").position is None
    assert _cover("55", typ="roleta_lamelki").position == 55


def test_record_survives_replace() -> None:
    obj = _object(id=1, record=DesignerRecord(location="Potter"))
    assert replace(obj, state="1").record == DesignerRecord(location="Potter")


def test_leaf_io_no_parses_last_segment() -> None:
    assert _object(id=1, leaf_id="0_cb89_257_2_7").leaf_io_no == 7
    assert _object(id=1, leaf_id="0_cb89_257_2_0").leaf_io_no == 0
    assert _object(id=1, leaf_id="").leaf_io_no is None
    assert _object(id=1, leaf_id="0_cb89_257_2_x").leaf_io_no is None
    assert _object(id=1, leaf_id="junk").leaf_io_no is None


def test_sf_id_and_sub_sf_id_parse_the_middle_segments():
    """sf_id and sub_sf_id read the third and fourth leaf_id segments."""
    obj = _object(id=1, leaf_id="0_1f2e_257_2_5")
    assert obj.sf_id == 257
    assert obj.sub_sf_id == 2


def test_sf_id_reads_none_for_a_malformed_leaf_id():
    """A leaf_id that does not parse yields None on every segment."""
    obj = _object(id=1, leaf_id="not-a-leaf")
    assert obj.sf_id is None
    assert obj.sub_sf_id is None
    assert obj.leaf_io_no is None


def test_sf_id_reads_none_for_an_empty_leaf_id():
    """System objects and Matter-unchecked rows carry an empty leaf_id."""
    obj = _object(id=1, leaf_id="")
    assert obj.sf_id is None
    assert obj.sub_sf_id is None


def test_a_leafless_alarm_object_keeps_the_alarm_family():
    """Designer clears leafId on a Matter uncheck, so sub_sf_id reads None
    on a row that did not change otherwise. The leaf refines the name and
    never decides the family."""
    leafless = _object(id=1, typ_komponentu="satel_alarm", leaf_id="")
    assert leafless.sub_sf_id is None
    assert leafless.kind == InputKind("alarm", "Alarm")
    armed = _object(id=1, typ_komponentu="satel_alarm", leaf_id="0_1f2e_296_3_0")
    assert armed.kind == InputKind("alarm_armed", "Alarm armed")


def test_sf_id_reads_none_when_the_segment_is_not_a_number():
    """A non-numeric segment yields None rather than raising."""
    obj = _object(id=1, leaf_id="0_1f2e_abc_2_5")
    assert obj.sf_id is None
    assert obj.sub_sf_id == 2


def test_record_bundles_default_to_none() -> None:
    assert _object(id=1).record is None
    assert _module(id=1).record is None


def test_record_bundle_fields_default_to_none() -> None:
    assert DesignerRecord() == DesignerRecord(
        location=None, matter_device_type=None, desc=None
    )
    assert ModuleRecord() == ModuleRecord(location=None, desc=None)


# --- the two presence rows ---------------------------------------------------


def test_presence_types_carry_exactly_their_fields() -> None:
    from dataclasses import fields

    from ampio_mqtt import PresenceDetection, PresenceSimulation

    assert [f.name for f in fields(PresenceDetection)] == ["id", "name", "home_status"]
    assert [f.name for f in fields(PresenceSimulation)] == ["id", "name", "active"]


def test_presence_types_have_no_boolean_reading() -> None:
    from ampio_mqtt import PresenceDetection, PresenceSimulation

    detection = PresenceDetection(id=15, name="Detection", home_status=5)
    simulation = PresenceSimulation(id=14, name="Simulation", active=False)
    for row in (detection, simulation):
        assert not hasattr(row, "is_on")
        assert not hasattr(row, "state")


def test_presence_changed_is_a_store_event() -> None:
    from ampio_mqtt import PresenceChanged, PresenceDetection
    from ampio_mqtt.events import StoreEvent

    event = PresenceChanged(
        detection=PresenceDetection(id=15, name=None, home_status=None),
        simulation=None,
    )
    assert isinstance(event, StoreEvent)
    assert event == PresenceChanged(
        detection=PresenceDetection(id=15, name=None, home_status=None),
        simulation=None,
    )
