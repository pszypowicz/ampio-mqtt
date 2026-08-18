"""Tests for the pure AmpioObject model properties - no client, no broker."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ampio_mqtt import AmpioObject, classify


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


def test_is_thermostat_and_the_running_flag() -> None:
    obj = AmpioObject(id=138, typ_komponentu="reg", kind=classify("reg", None))
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
