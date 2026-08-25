"""Tests for the pure AmpioObject model properties - no client, no broker."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ampio_mqtt import AmpioModule, AmpioObject, AmpioServerInfo
from ampio_mqtt.classification import classify
from ampio_mqtt.device_types import module_model


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


@pytest.mark.parametrize(
    ("leaf_id", "expected"),
    [("0_cb9b_74_0_1", "leaf_0_cb9b_74_0_1"), ("", None)],
)
def test_stable_key_from_leaf_id(leaf_id: str, expected: str | None) -> None:
    assert AmpioObject(id=1, leaf_id=leaf_id).stable_key == expected


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
    module = AmpioModule(id=1, type=4)
    assert module.model == module_model(4)
    assert module.model is not None
    assert replace(module, type=None).model is None
    with pytest.raises(TypeError):
        AmpioModule(id=1, model="M-REL")  # type: ignore[call-arg]


def test_is_thermostat_and_the_running_flag() -> None:
    obj = AmpioObject(id=138, typ_komponentu="reg")
    assert obj.is_thermostat
    assert not (obj.is_sensor or obj.is_input or obj.is_output)
    assert replace(obj, value="1").is_on  # the surfaced value is the running flag


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
    """`0_<macHex>_<F2>_<F3>_<F4>` yields the module's override mac; any
    other shape yields None rather than a half-parsed guess."""
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
    return AmpioObject(id=1, typ_komponentu="rgbw", value=value)


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
    dimmer = AmpioObject(id=1, typ_komponentu="led", value="255")
    assert dimmer.rgbw is None


def _cover(value: str | None, typ: str = "roleta_procenty") -> AmpioObject:
    return AmpioObject(id=1, typ_komponentu=typ, value=value)


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
