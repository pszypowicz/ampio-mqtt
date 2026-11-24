"""Tests for DB-object classification."""

from __future__ import annotations

import pytest

from ampio_mqtt import ModuleAddress
from ampio_mqtt.classification import (
    INPUT_KIND_KEYS,
    OUTPUT_KIND_KEYS,
    SENSOR_KIND_KEY_PREFIXES,
    SENSOR_KIND_KEYS,
    THERMOSTAT_KIND_KEYS,
    TYPE_PROFILES,
    InputKind,
    OutputKind,
    SensorKind,
    ThermostatKind,
    classify,
    input_channel_prefix,
)


def _leaf(sf_id: int, sub_sf_id: int = 0) -> ModuleAddress:
    return ModuleAddress(mac=0x1, channel=0, sf_id=sf_id, sub_sf_id=sub_sf_id)


# A binary I/O leaf, for the tests where the leaf plays no part in the kind.
_IO_LEAF = _leaf(257)


def _sensor(
    typ: str | None, interp: int | None, leaf: ModuleAddress = _IO_LEAF
) -> SensorKind | None:
    kind = classify(typ, interp, leaf)
    return kind if isinstance(kind, SensorKind) else None


def _input(typ: str | None, interp: int | None) -> InputKind | None:
    kind = classify(typ, interp, _IO_LEAF)
    return kind if isinstance(kind, InputKind) else None


def test_temperature() -> None:
    kind = _sensor("temp", 1)
    assert kind is not None
    assert kind.key == "temperature"
    assert kind.unit == "°C"
    assert kind.device_class == "temperature"


@pytest.mark.parametrize(
    ("typ", "interp", "sf_id", "precision"),
    [
        ("temp", 1, 76, 1),
        ("lin_wej", 1, 73, 1),  # humidity
        ("lin_wej", 4, 74, 0),  # illuminance
        ("lin_wej", 5, 74, 0),  # iaq
        ("lin_wej", 7, 75, 0),  # co2
        ("lin_wej", 7, 1003, 1),  # a bridge slot
        ("bit32", 3, 1005, 1),
        (None, None, 257, None),  # unknown fallback: no precision hint
    ],
)
def test_display_precision(
    typ: str | None, interp: int | None, sf_id: int, precision: int | None
) -> None:
    kind = _sensor(typ, interp, _leaf(sf_id))
    assert kind is not None
    assert kind.precision == precision


@pytest.mark.parametrize(
    ("sf_id", "interp", "key", "unit", "device_class"),
    [
        (73, 1, "humidity", "%", "humidity"),
        (73, 2, "pressure_abs", "hPa", "atmospheric_pressure"),
        (73, 3, "loudness", "dB", "sound_pressure"),
        (74, 4, "illuminance", "lx", "illuminance"),
        (74, 5, "iaq", None, "aqi"),
        (74, 6, "pressure_rel", "hPa", "pressure"),
        (75, 7, "co2", "ppm", "carbon_dioxide"),
    ],
)
def test_msens_lin_wej_channels(
    sf_id: int, interp: int, key: str, unit: str | None, device_class: str
) -> None:
    """Designer numbers the M-SENS channels 1 to 7 across its three lin_wej
    leaf classes, and each number names one measurement."""
    kind = _sensor("lin_wej", interp, _leaf(sf_id))
    assert kind is not None
    assert (kind.key, kind.unit, kind.device_class) == (key, unit, device_class)


@pytest.mark.parametrize(("sf_id", "interp"), [(73, 42), (75, 8), (74, None)])
def test_an_unmapped_msens_number_is_a_value_slot(
    sf_id: int, interp: int | None
) -> None:
    """On an M-SENS class, a number outside 1 to 7 or no number names no
    measurement, so the object is the same open value slot as an integer
    slot."""
    kind = classify("lin_wej", interp, _leaf(sf_id))
    assert kind == classify("bit8", interp, _leaf(1002))


@pytest.mark.parametrize("sf_id", [20, 285, 1003])
@pytest.mark.parametrize("interp", [1, 7, 9])
def test_a_lin_wej_off_the_msens_classes_is_a_value_slot(
    sf_id: int, interp: int
) -> None:
    """A generic analog input (20), a 0-10 V input (285) and a bridge slot
    (1003) hold a plain channel number, so channel 7 is not CO2. The object
    is the same open value slot as an integer slot, with no unit of its
    own."""
    kind = classify("lin_wej", interp, _leaf(sf_id))
    assert kind == classify("bit8", interp, _leaf(1002))
    assert isinstance(kind, SensorKind)
    assert (kind.key, kind.unit, kind.device_class) == (f"value_{interp}", None, None)


@pytest.mark.parametrize("typ", [t for t in TYPE_PROFILES if t != "lin_wej"])
def test_the_leaf_class_refines_lin_wej_alone(typ: str) -> None:
    assert classify(typ, 1, _leaf(1003)) == classify(typ, 1, _leaf(73))


def test_bit32_is_generic_measurement() -> None:
    kind = _sensor("bit32", 3)
    assert kind is not None
    assert kind.device_class is None
    assert kind.state_class == "measurement"


@pytest.mark.parametrize(
    "typ",
    [
        "przekaznik",
        "rgbw",
        "led",
        "roleta_procenty",
        "flaga",
        "wej",
    ],
)
def test_non_sensor_types(typ) -> None:
    assert _sensor(typ, 1) is None


def test_unknown_type_falls_back_to_generic() -> None:
    # No metadata (restricted account) -> generic value sensor.
    kind = _sensor(None, None)
    assert kind is not None
    assert kind.key == "value"


@pytest.mark.parametrize(
    ("typ", "key"),
    [
        ("flaga", "flaga"),  # generic boolean
        ("wej", "wej"),  # physical input, generic boolean (#117)
    ],
)
def test_classify_input_types(typ, key) -> None:
    kind = _input(typ, 1)
    assert kind is not None
    assert kind.key == key


@pytest.mark.parametrize(
    ("typ", "switchable"),
    [
        ("flaga", True),
        ("wej", False),
    ],
)
def test_input_switchability(typ: str, switchable: bool) -> None:
    """Only `flaga` answers the switch verbs. A `wej` ignores them on both
    account tiers."""
    kind = _input(typ, 1)
    assert kind is not None
    assert kind.switchable is switchable


@pytest.mark.parametrize(
    ("typ", "pulsable"),
    [
        ("flaga", True),
        ("flaga_liniowa", False),
        ("flaga_liniowa16", False),
        ("wej", False),
    ],
)
def test_input_pulsability(typ: str, pulsable: bool) -> None:
    """A `setValue` time argument reverts a plain `flaga`. Both analog
    flags take the timed form, set the value and latch."""
    kind = _input(typ, 1)
    assert kind is not None
    assert kind.pulsable is pulsable


def _output(typ, interp=1):
    kind = classify(typ, interp, _IO_LEAF)
    return kind if isinstance(kind, OutputKind) else None


@pytest.mark.parametrize(
    ("typ", "key", "dimmable", "color", "cover", "position", "tilt"),
    [
        ("przekaznik", "relay", False, False, False, False, False),
        ("led", "dimmer", True, False, False, False, False),
        ("rgbw", "rgbw", False, True, False, False, False),
        ("ledww", "cct", False, False, False, False, False),
        ("roleta", "cover", False, False, True, False, False),
        ("roleta_procenty", "cover_position", False, False, True, True, False),
        ("roleta_lamelki", "cover_tilt", False, False, True, True, True),
    ],
)
def test_output_kinds(typ, key, dimmable, color, cover, position, tilt) -> None:
    out = _output(typ)
    assert out is not None
    assert out.key == key
    assert (out.dimmable, out.color) == (dimmable, color)
    assert (out.cover, out.position, out.tilt) == (cover, position, tilt)


def test_ledww_is_the_color_temperature_output() -> None:
    """`color_temp` is its own axis: a CCT light is neither an RGBW color
    output nor a dimmer, so it must claim neither flag."""
    out = _output("ledww")
    assert out is not None
    assert out.color_temp is True
    assert (out.color, out.dimmable) == (False, False)


@pytest.mark.parametrize(
    ("typ", "switchable", "toggleable"),
    [
        ("przekaznik", True, True),
        ("led", True, True),
        ("roleta_procenty", True, True),
        # `rgbw` answers none of the three; `ledww` answers `switch` alone.
        ("rgbw", False, False),
        ("ledww", False, True),
    ],
)
def test_output_switch_verb_families(
    typ: str, switchable: bool, toggleable: bool
) -> None:
    """`switchable` covers `turnOn`/`turnOff` and `toggleable` covers
    `switch`. They diverge on `ledww`, which is why they are separate
    fields."""
    out = _output(typ)
    assert out is not None
    assert (out.switchable, out.toggleable) == (switchable, toggleable)


@pytest.mark.parametrize(
    ("typ", "pulsable"),
    [
        ("przekaznik", True),
        ("led", True),
        # `rgbw` and the covers answer no `setValue` at all. A `ledww`
        # answers the timed form, and it zeroes the coldness rather than
        # reverting.
        ("rgbw", False),
        ("ledww", False),
        ("roleta_procenty", False),
    ],
)
def test_output_pulsability(typ: str, pulsable: bool) -> None:
    """`pulsable` names the outputs a `setValue` time argument reverts."""
    out = _output(typ)
    assert out is not None
    assert out.pulsable is pulsable


@pytest.mark.parametrize(
    ("typ", "expected"),
    [
        ("temp", SensorKind),
        ("lin_wej", SensorKind),
        ("bit32", SensorKind),
        ("flaga", InputKind),
        ("wej", InputKind),
        ("przekaznik", OutputKind),
        ("led", OutputKind),
        ("rgbw", OutputKind),
        ("roleta", OutputKind),
        ("roleta_procenty", OutputKind),
        ("roleta_lamelki", OutputKind),
        (None, SensorKind),  # no metadata yet -> the generic value sensor
        ("nonsense", SensorKind),  # unknown type, same fallback
    ],
)
def test_every_type_maps_to_exactly_one_kind(typ: str | None, expected: type) -> None:
    """An object is a measurement, a boolean input, or controllable - never two."""
    assert isinstance(classify(typ, 1, _IO_LEAF), expected)


def test_reg_is_a_thermostat() -> None:
    kind = classify("reg", None, _leaf(13))
    assert isinstance(kind, ThermostatKind)
    assert kind.key == "thermostat"


@pytest.mark.parametrize("typ", ["bit16", "sbit16"])
def test_16_bit_slots_are_numeric_measurements(typ: str) -> None:
    """The M-CON-485 integer slots Designer names `bit 16` and `sbit 16[+/-]`:
    the same open family as bit8 and bit32."""
    kind = classify(typ, 3, _IO_LEAF)
    assert isinstance(kind, SensorKind)
    assert (kind.key, kind.name) == ("value_3", "Measurement")


def test_bit8_is_a_numeric_measurement() -> None:
    """Same treatment as its bit32 sibling: a generic numeric sensor keyed
    by interpretacja."""
    kind = classify("bit8", 3, _leaf(1002))
    assert isinstance(kind, SensorKind)
    assert (kind.key, kind.name) == ("value_3", "Measurement")


# --- the exported kind-key vocabulary --------------------------------------


def test_kind_key_vocabulary_contents() -> None:
    """The exhaustiveness tripwire: adding a kind must update this list,
    exactly as a consumer's own mapping test will demand of its mapping."""
    assert {
        "flaga",
        "flaga_liniowa",
        "flaga_liniowa16",
        "alarm",
        "alarm_armed",
        "alarm_alarmed",
        "wej",
    } == INPUT_KIND_KEYS
    assert {
        "relay",
        "rgbw",
        "cct",
        "dimmer",
        "cover",
        "cover_position",
        "cover_tilt",
    } == OUTPUT_KIND_KEYS
    assert {"thermostat"} == THERMOSTAT_KIND_KEYS
    assert {
        "value",
        "temperature",
        "humidity",
        "pressure_abs",
        "loudness",
        "illuminance",
        "iaq",
        "pressure_rel",
        "co2",
    } == SENSOR_KIND_KEYS
    assert SENSOR_KIND_KEY_PREFIXES == ("value_",)


def test_classify_never_leaves_the_exported_vocabulary() -> None:
    """Every key classify() can mint is either exported or in an exported
    open family - the invariant a consumer's exhaustiveness check rests on."""
    vocab = {
        SensorKind: SENSOR_KIND_KEYS,
        InputKind: INPUT_KIND_KEYS,
        OutputKind: OUTPUT_KIND_KEYS,
        ThermostatKind: THERMOSTAT_KIND_KEYS,
    }
    leaves = [_leaf(sf_id) for sf_id in (20, 73, 74, 75, 257, 1003)]
    leaves += [_leaf(296, sub_sf_id) for sub_sf_id in (3, 4, 9)]
    for typ in [None, "no_such_type", *TYPE_PROFILES]:
        for interp in [None, *range(9), 99]:
            for leaf in leaves:
                kind = classify(typ, interp, leaf)
                key = kind.key
                assert key in vocab[type(kind)] or key.startswith(
                    SENSOR_KIND_KEY_PREFIXES
                ), (typ, interp, leaf, key)


# --- the analog flags and the alarm halves (#239) ---------------------------


@pytest.mark.parametrize(
    ("typ", "key", "low", "high"),
    [
        ("flaga_liniowa", "flaga_liniowa", 0, 255),
        ("flaga_liniowa16", "flaga_liniowa16", -32768, 32767),
    ],
)
def test_analog_flags_carry_their_own_width(
    typ: str, key: str, low: int, high: int
) -> None:
    """Both answer setValue, and the 16-bit one is signed, so one shared
    0-255 range would reject half of its legal values."""
    kind = classify(typ, 1, _IO_LEAF)
    assert isinstance(kind, InputKind)
    assert kind.key == key
    assert kind.value_range == (low, high)
    # A flag holds a value; it does not answer the switch verbs.
    assert kind.switchable is False


@pytest.mark.parametrize(
    ("sub_sf_id", "key"),
    [(3, "alarm_armed"), (4, "alarm_alarmed")],
)
def test_alarm_halves_split_on_the_leaf_sub_function(sub_sf_id: int, key: str) -> None:
    """The catalogue row cannot tell the halves apart: both carry the same
    typ_komponentu, funkcja and interpretacja. Only the leaf differs."""
    kind = classify("satel_alarm", 1, _leaf(296, sub_sf_id))
    assert isinstance(kind, InputKind)
    assert kind.key == key


@pytest.mark.parametrize("sub_sf_id", [1, 2, 9])
def test_an_unproven_alarm_sub_function_keeps_the_family(sub_sf_id: int) -> None:
    """typ_komponentu alone decides the family, so an object with no proven
    half stays an alarm input. The leaf only refines the name."""
    kind = classify("satel_alarm", 1, _leaf(296, sub_sf_id))
    assert isinstance(kind, InputKind)
    assert (kind.key, kind.name) == ("alarm", "Alarm")


def test_a_sub_function_outside_the_two_halves_is_the_base_alarm() -> None:
    assert classify("satel_alarm", 0, _leaf(296)).key == "alarm"
    assert classify("satel_alarm", 0, _leaf(296, 9)).key == "alarm"


@pytest.mark.parametrize(
    ("typ", "prefix"),
    [("flaga", "f"), ("wej", "i"), ("flaga_liniowa", None), ("flaga_liniowa16", None)],
)
def test_an_input_carries_a_raw_prefix_only_when_the_client_subscribes_it(
    typ: str, prefix: str | None
) -> None:
    """Every prefix an input profile sets is one the admin client subscribes.
    The analog flags have none, so they update on the per-object topic
    (#292)."""
    assert input_channel_prefix(typ) == prefix
