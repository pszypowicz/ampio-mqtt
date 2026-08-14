"""Tests for DB-object classification."""

from __future__ import annotations

import pytest

from ampio_mqtt import classify
from ampio_mqtt.const import InputKind, SensorKind


def _sensor(typ: str | None, interp: int | None) -> SensorKind | None:
    return classify(typ, interp)[0]


def _input(typ: str | None, interp: int | None) -> InputKind | None:
    return classify(typ, interp)[1]


def test_temperature() -> None:
    kind = _sensor("temp", 1)
    assert kind is not None
    assert kind.key == "temperature"
    assert kind.unit == "°C"
    assert kind.device_class == "temperature"
    assert kind.precision == 1


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


@pytest.mark.parametrize(
    "typ",
    ["temp", "lin_wej", "bit32", "przekaznik", "rgbw", "led", "roleta_procenty", None],
)
def test_classify_input_returns_none_for_non_inputs(typ) -> None:
    assert _input(typ, 1) is None


def test_input_and_sensor_classifications_are_disjoint() -> None:
    """An input type is never also a sensor, and vice versa."""
    for typ in ("flaga", "detekcja", "symulacja"):
        sensor, inp, _ = classify(typ, 1)
        assert sensor is None
        assert inp is not None


# --- output classification -------------------------------------------------


def _output(typ, interp=1):
    return classify(typ, interp)[2]


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


@pytest.mark.parametrize("typ", ["temp", "lin_wej", "bit32", "flaga", "detekcja", None])
def test_non_outputs_have_no_output_kind(typ) -> None:
    assert _output(typ) is None


@pytest.mark.parametrize(
    "typ", ["przekaznik", "led", "rgbw", "roleta", "roleta_procenty", "roleta_lamelki"]
)
def test_outputs_are_not_sensors(typ) -> None:
    """An output must not fall through to the generic value sensor."""
    sensor, inp, out = classify(typ, 1)
    assert sensor is None
    assert inp is None
    assert out is not None
