"""Tests for DB-object classification."""

from __future__ import annotations

import pytest

from aioampio import classify_object


def test_temperature() -> None:
    kind = classify_object("temp", 1)
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
    kind = classify_object(typ, interp)
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
    kind = classify_object("lin_wej", interp)
    assert kind is not None
    assert kind.key == key
    assert kind.unit == unit
    assert kind.device_class == device_class


def test_lin_wej_unknown_interp_is_generic() -> None:
    kind = classify_object("lin_wej", 42)
    assert kind is not None
    assert kind.device_class is None


def test_bit32_is_generic_measurement() -> None:
    kind = classify_object("bit32", 3)
    assert kind is not None
    assert kind.device_class is None
    assert kind.state_class == "measurement"


@pytest.mark.parametrize(
    "typ", ["przekaznik", "rgbw", "led", "roleta_procenty", "flaga", "detekcja", "symulacja"]
)
def test_non_sensor_types(typ) -> None:
    assert classify_object(typ, 1) is None


def test_unknown_type_falls_back_to_generic() -> None:
    # No metadata (restricted account) -> generic value sensor.
    kind = classify_object(None, None)
    assert kind is not None
    assert kind.key == "value"
