"""Tests for DB-object classification."""

from __future__ import annotations

import pytest

from ampio_mqtt.classification import (
    INPUT_KIND_KEYS,
    OPEN_SENSOR_KEY_PREFIXES,
    OUTPUT_KIND_KEYS,
    SENSOR_KIND_KEYS,
    THERMOSTAT_KIND_KEYS,
    TYPE_PROFILES,
    InputKind,
    OutputKind,
    SensorKind,
    ThermostatKind,
    classify,
)


def _sensor(typ: str | None, interp: int | None) -> SensorKind | None:
    kind = classify(typ, interp)
    return kind if isinstance(kind, SensorKind) else None


def _input(typ: str | None, interp: int | None) -> InputKind | None:
    kind = classify(typ, interp)
    return kind if isinstance(kind, InputKind) else None


def test_temperature() -> None:
    kind = _sensor("temp", 1)
    assert kind is not None
    assert kind.key == "temperature"
    assert kind.unit == "°C"
    assert kind.device_class == "temperature"


@pytest.mark.parametrize(
    ("typ", "interp", "precision"),
    [
        ("temp", 1, 1),
        ("lin_wej", 1, 1),  # humidity
        ("lin_wej", 4, 0),  # illuminance
        ("lin_wej", 5, 0),  # iaq
        ("lin_wej", 7, 0),  # co2
        ("bit32", 3, 1),
        (None, None, None),  # unknown fallback: no precision hint
    ],
)
def test_display_precision(typ, interp, precision) -> None:
    kind = _sensor(typ, interp)
    assert kind is not None
    assert kind.precision == precision


@pytest.mark.parametrize(
    ("interp", "key", "unit", "device_class"),
    [
        (1, "humidity", "%", "humidity"),
        (2, "pressure_abs", "hPa", "atmospheric_pressure"),
        (3, "loudness", "dB", "sound_pressure"),
        (4, "illuminance", "lx", "illuminance"),
        (5, "iaq", None, "aqi"),
        (6, "pressure_rel", "hPa", "pressure"),
        (7, "co2", "ppm", "carbon_dioxide"),
    ],
)
def test_lin_wej_channels(interp, key, unit, device_class) -> None:
    kind = _sensor("lin_wej", interp)
    assert kind is not None
    assert kind.key == key
    assert kind.unit == unit
    assert kind.device_class == device_class


def test_lin_wej_unknown_interp_is_generic() -> None:
    kind = _sensor("lin_wej", 42)
    assert kind is not None
    assert kind.device_class is None


def test_bit32_is_generic_measurement() -> None:
    kind = _sensor("bit32", 3)
    assert kind is not None
    assert kind.device_class is None
    assert kind.state_class == "measurement"


@pytest.mark.parametrize(
    "typ",
    ["przekaznik", "rgbw", "led", "roleta_procenty", "flaga", "detekcja", "symulacja"],
)
def test_non_sensor_types(typ) -> None:
    assert _sensor(typ, 1) is None


def test_unknown_type_falls_back_to_generic() -> None:
    # No metadata (restricted account) -> generic value sensor.
    kind = _sensor(None, None)
    assert kind is not None
    assert kind.key == "value"


@pytest.mark.parametrize(
    ("typ", "key", "device_class"),
    [
        ("flaga", "flaga", None),  # generic boolean
        ("detekcja", "detekcja", "motion"),
        ("symulacja", "symulacja", None),  # generic boolean
    ],
)
def test_classify_input_types(typ, key, device_class) -> None:
    kind = _input(typ, 1)
    assert kind is not None
    assert kind.key == key
    assert kind.device_class == device_class


def _output(typ, interp=1):
    kind = classify(typ, interp)
    return kind if isinstance(kind, OutputKind) else None


@pytest.mark.parametrize(
    ("typ", "key", "dimmable", "color", "cover", "position", "tilt"),
    [
        ("przekaznik", "relay", False, False, False, False, False),
        ("led", "dimmer", True, False, False, False, False),
        ("rgbw", "rgbw", False, True, False, False, False),
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


@pytest.mark.parametrize(
    ("typ", "expected"),
    [
        ("temp", SensorKind),
        ("lin_wej", SensorKind),
        ("bit32", SensorKind),
        ("flaga", InputKind),
        ("detekcja", InputKind),
        ("symulacja", InputKind),
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
    assert isinstance(classify(typ, 1), expected)


def test_reg_is_a_thermostat() -> None:
    kind = classify("reg", None)
    assert isinstance(kind, ThermostatKind)
    assert kind.key == "thermostat"


def test_bit8_is_a_numeric_measurement() -> None:
    """Same treatment as its bit32 sibling: a generic numeric sensor keyed
    by interpretacja."""
    kind = classify("bit8", 3)
    assert isinstance(kind, SensorKind)
    assert (kind.key, kind.name) == ("value_3", "Measurement")


# --- the exported kind-key vocabulary --------------------------------------


def test_kind_key_vocabulary_contents() -> None:
    """The exhaustiveness tripwire: adding a kind must update this list,
    exactly as a consumer's own mapping test will demand of its mapping."""
    assert {"flaga", "detekcja", "symulacja"} == INPUT_KIND_KEYS
    assert {
        "relay",
        "rgbw",
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


def test_classify_never_leaves_the_exported_vocabulary() -> None:
    """Every key classify() can mint is either exported or in an exported
    open family - the invariant a consumer's exhaustiveness check rests on."""
    vocab = {
        SensorKind: SENSOR_KIND_KEYS,
        InputKind: INPUT_KIND_KEYS,
        OutputKind: OUTPUT_KIND_KEYS,
        ThermostatKind: THERMOSTAT_KIND_KEYS,
    }
    for typ in [None, "no_such_type", *TYPE_PROFILES]:
        for interp in [None, *range(9), 99]:
            kind = classify(typ, interp)
            key = kind.key
            assert key in vocab[type(kind)] or key.startswith(
                OPEN_SENSOR_KEY_PREFIXES
            ), (typ, interp, key)
