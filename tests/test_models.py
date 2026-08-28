"""Tests for the pure AmpioObject model properties - no client, no broker."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ampio_mqtt import AmpioModule, AmpioObject, AmpioServerInfo
from ampio_mqtt.classification import ThermostatKind, classify
from ampio_mqtt.device_types import module_model
from ampio_mqtt.models import DesignerRecord, ModuleRecord


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("1", True), ("255", True)],
)
def test_is_on_interpretation(value, expected) -> None:
    assert AmpioObject(id=1, state=value).is_on is expected


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
    assert AmpioObject(id=1, state=value).numeric_value == expected


@pytest.mark.parametrize(
    ("leaf_id", "expected"),
    [("0_cb9b_74_0_1", "leaf_0_cb9b_74_0_1"), ("", None)],
)
def test_stable_key_from_leaf_id(leaf_id: str, expected: str | None) -> None:
    assert AmpioObject(id=1, leaf_id=leaf_id).stable_key == expected


def test_unique_key_is_the_object_id() -> None:
    assert AmpioObject(id=150, leaf_id="0_be82_257_2_2").unique_key == "obj_150"


def test_unique_key_separates_views_of_one_output() -> None:
    """Two Designer views of one output share a leaf but not an identity."""
    leaf = "0_be82_257_2_2"
    relay_view = AmpioObject(id=150, leaf_id=leaf)
    bell_view = AmpioObject(id=151, leaf_id=leaf)
    assert relay_view.stable_key == bell_view.stable_key
    assert relay_view.unique_key != bell_view.unique_key


def test_unique_key_survives_an_empty_leaf_id() -> None:
    """System objects and ghost rows carry no leaf, but do carry an id."""
    assert AmpioObject(id=99, leaf_id="").unique_key == "obj_99"


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
    obj = AmpioObject(id=1, params=params)
    assert obj.read_only is read_only


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
    obj = AmpioObject(id=1, typ_komponentu=typ, params=params)
    assert obj.bell is bell


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


# --- derived fields: kind and model own their inputs (#94) ------------------


def test_kind_derives_from_the_metadata_inputs() -> None:
    """A seeded instance carries the same kind the store would compute -
    the derivation lives in the model, not at every construction site."""
    assert AmpioObject(id=1, typ_komponentu="led").kind == classify("led", None)
    assert AmpioObject(id=1).kind == classify(None, None)  # the generic sensor


def test_kind_rederives_on_replace() -> None:
    obj = AmpioObject(id=1, typ_komponentu="led")
    assert replace(obj, typ_komponentu="rgbw").kind == classify("rgbw", None)


def test_kind_cannot_be_passed() -> None:
    """No instance can hold a kind that disagrees with its inputs."""
    with pytest.raises(TypeError):
        AmpioObject(id=1, kind=classify("led", None))  # type: ignore[call-arg]


def test_module_model_derives_from_type() -> None:
    module = AmpioModule(id=1, typ_urzadzenia=4)
    assert module.model == module_model(4)
    assert module.model is not None
    assert replace(module, typ_urzadzenia=None).model is None
    with pytest.raises(TypeError):
        AmpioModule(id=1, model="M-REL")  # type: ignore[call-arg]


def test_reg_classifies_as_thermostat_and_surfaces_the_running_flag() -> None:
    obj = AmpioObject(id=138, typ_komponentu="reg")
    assert isinstance(obj.kind, ThermostatKind)
    assert replace(obj, state="1").is_on  # the surfaced value is the running flag


@pytest.mark.parametrize(
    ("leaf_id", "expected"),
    [
        ("0_cb8f_76_0_0", 0xCB8F),
        ("0_1_10_0_0", 1),  # the M-SERV's override mac, not its factory id
        ("0_D09A_5_1_2", 0xD09A),  # uppercase hex parses too
        ("", None),  # system objects and ghost rows carry no leafId
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
    assert AmpioObject(id=1, leaf_id=leaf_id).module_mac == expected


@pytest.mark.parametrize(
    ("leaf_id", "server_owned"),
    [
        ("0_1_10_0_0", True),  # the M-SERV's override mac
        ("0_cb8f_76_0_0", False),  # another module's object
        ("", False),  # system objects and ghost rows
    ],
)
def test_is_server_owned_reads_the_mserv_override_mac(
    leaf_id: str, server_owned: bool
) -> None:
    """Served identically on both tiers via leafId, so server-owned
    objects anchor to the hub device without a module catalogue."""
    assert AmpioObject(id=1, leaf_id=leaf_id).is_server_owned is server_owned


@pytest.mark.parametrize(("mac", "expected"), [(47846, "47846"), (1, "1")])
def test_server_key_is_the_decimal_mac(mac: int, expected: str) -> None:
    """The canonical registry-scoping string; its format is a promise."""
    assert AmpioServerInfo(mac=mac).key == expected


def _colored(value: str | None) -> AmpioObject:
    return AmpioObject(id=1, typ_komponentu="rgbw", state=value)


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
    dimmer = AmpioObject(id=1, typ_komponentu="led", state="255")
    assert dimmer.rgbw is None


def _cover(value: str | None, typ: str = "roleta_procenty") -> AmpioObject:
    return AmpioObject(id=1, typ_komponentu=typ, state=value)


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
    obj = AmpioObject(id=1, record=DesignerRecord(location="Potter"))
    assert replace(obj, state="1").record == DesignerRecord(location="Potter")


def test_leaf_io_no_parses_last_segment() -> None:
    assert AmpioObject(id=1, leaf_id="0_cb89_257_2_7").leaf_io_no == 7
    assert AmpioObject(id=1, leaf_id="0_cb89_257_2_0").leaf_io_no == 0
    assert AmpioObject(id=1, leaf_id="").leaf_io_no is None
    assert AmpioObject(id=1, leaf_id="0_cb89_257_2_x").leaf_io_no is None
    assert AmpioObject(id=1, leaf_id="junk").leaf_io_no is None


def test_sf_id_and_sub_sf_id_parse_the_middle_segments():
    """sf_id and sub_sf_id read the third and fourth leaf_id segments."""
    obj = AmpioObject(id=1, leaf_id="0_1f2e_257_2_5")
    assert obj.sf_id == 257
    assert obj.sub_sf_id == 2


def test_sf_id_reads_none_for_a_malformed_leaf_id():
    """A leaf_id that does not parse yields None on every segment."""
    obj = AmpioObject(id=1, leaf_id="not-a-leaf")
    assert obj.sf_id is None
    assert obj.sub_sf_id is None
    assert obj.leaf_io_no is None


def test_sf_id_reads_none_for_an_empty_leaf_id():
    """System objects and ghost rows carry an empty leaf_id."""
    obj = AmpioObject(id=1, leaf_id="")
    assert obj.sf_id is None
    assert obj.sub_sf_id is None


def test_sf_id_reads_none_when_the_segment_is_not_a_number():
    """A non-numeric segment yields None rather than raising."""
    obj = AmpioObject(id=1, leaf_id="0_1f2e_abc_2_5")
    assert obj.sf_id is None
    assert obj.sub_sf_id == 2


def test_record_bundles_default_to_none() -> None:
    assert AmpioObject(id=1).record is None
    assert AmpioModule(id=1).record is None


def test_record_bundle_fields_default_to_none() -> None:
    assert DesignerRecord() == DesignerRecord(
        location=None, matter_device_type=None, desc=None
    )
    assert ModuleRecord() == ModuleRecord(location=None, desc=None)
