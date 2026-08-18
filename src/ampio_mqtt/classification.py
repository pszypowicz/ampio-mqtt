"""Object classification for the Ampio DB-object protocol.

One `TypeProfile` row per known ``typ_komponentu`` drives everything the
library derives from a component type: its sensor/input/output kind, the
raw-channel bridge prefix, and the system-object marker. This module is
Home Assistant agnostic; device/state class strings match Home Assistant's
SensorDeviceClass / SensorStateClass enum values so consumers can pass
them through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Device-class strings the library can emit. Values match Home Assistant's
# SensorDeviceClass enum so consumers may pass them through directly.
DeviceClass = Literal[
    "atmospheric_pressure",
    "aqi",
    "carbon_dioxide",
    "humidity",
    "illuminance",
    "pressure",
    "sound_pressure",
    "temperature",
]

# State-class strings. Values match Home Assistant's SensorStateClass.
StateClass = Literal["measurement", "total", "total_increasing"]


@dataclass(frozen=True, slots=True)
class SensorKind:
    """Neutral description of a sensor measurement."""

    key: str
    name: str
    unit: str | None
    device_class: DeviceClass | None
    state_class: StateClass | None = "measurement"
    # Display precision hint. The protocol reports float32 noise (e.g. 2702.7
    # arrives as "2702.699951"); 1 decimal matches the device's own `desc`
    # field. 0 for quantities conventionally shown as integers.
    precision: int | None = 1


# binary_sensor device-class strings the library can emit. Only "motion" is
# mapped today; extend this Literal when a new input mapping is added. Values
# match Home Assistant's BinarySensorDeviceClass enum.
BinarySensorDeviceClass = Literal["motion"]


@dataclass(frozen=True, slots=True)
class InputKind:
    """Neutral description of a binary / flag-shaped input object."""

    key: str
    name: str
    # HA binary_sensor device class, or None for a generic boolean where the
    # consumer decides how to model it (binary_sensor vs switch).
    device_class: BinarySensorDeviceClass | None = None


@dataclass(frozen=True, slots=True)
class OutputKind:
    """Neutral description of a controllable output object.

    The flags say which command verbs the object answers, so a consumer can
    pick a platform and feature set without a `typ_komponentu` table of its
    own. ``switchable`` covers the ``turnOn`` / ``turnOff`` / ``switch``
    family: every output answers it except ``rgbw``, which the M-SERV
    drives through ``setColors`` alone.
    """

    key: str
    name: str
    # 0-255 level via `setValue`.
    dimmable: bool = False
    # Four RGBW channels via `setColors`.
    color: bool = False
    # `open` / `close` travel commands.
    cover: bool = False
    # Position axis of `setRollerPos`.
    position: bool = False
    # Lamella axis of `setRollerPos`, and a `lammel` field in the object's
    # state payload. The KEEP_POSITION sentinel (101) leaves an axis
    # uncommanded, exactly as on the position axis - see docs/protocol.md
    # and `AmpioClient.set_cover_tilt`, which relies on it.
    tilt: bool = False
    # The `turnOn` / `turnOff` / `switch` verb family. False only for `rgbw`:
    # the M-SERV ignores all three for it (no effect, no reply), so
    # `setColors` is its one switching verb - which `AmpioClient.turn_off`
    # relies on to emulate off.
    switchable: bool = True


# lin_wej (analog input) measurement kind, keyed by `interpretacja`.
# The M-SENS channel map (4=lux, 5=IAQ, 7=CO2).
_LIN_WEJ_BY_INTERP: dict[int, SensorKind] = {
    1: SensorKind("humidity", "Humidity", "%", "humidity"),
    2: SensorKind("pressure_abs", "Pressure (absolute)", "hPa", "atmospheric_pressure"),
    3: SensorKind("loudness", "Loudness", "dB", "sound_pressure"),
    4: SensorKind("illuminance", "Illuminance", "lx", "illuminance", precision=0),
    5: SensorKind("iaq", "Air quality index", None, "aqi", precision=0),
    6: SensorKind("pressure_rel", "Pressure (relative)", "hPa", "pressure"),
    7: SensorKind("co2", "CO2", "ppm", "carbon_dioxide", precision=0),
}

# Generic value-only sensor for an object with no usable metadata (a state
# push that raced ahead of the catalogues, or a `typ_komponentu` missing from
# TYPE_PROFILES). The value may be non-numeric, so it claims neither a state
# class nor a precision - both would make Home Assistant reject a text value.
_GENERIC_SENSOR = SensorKind(
    "value", "Value", None, None, state_class=None, precision=None
)


@dataclass(frozen=True, slots=True)
class ThermostatKind:
    """Neutral description of a temperature-controller (`reg`) object.

    Its state value is the running flag, not a measurement, and it accepts
    commands (:meth:`AmpioClient.set_temperature`) without answering the
    output verbs - so it is none of the other three kinds. The rich state
    the regulator pushes (measured and target temperature, mode, cooling)
    is not surfaced yet; #73 tracks the climate readback.
    """

    key: str
    name: str


# What an object is. Exactly one applies - a component type is a measurement,
# a boolean input, something controllable, or a thermostat, never two - so
# the kinds are alternatives rather than a set of optional slots.
ObjectKind = SensorKind | InputKind | OutputKind | ThermostatKind


@dataclass(frozen=True, slots=True)
class TypeProfile:
    """Everything the library derives from one ``typ_komponentu``.

    One row per known component type. A type absent from the table is unknown
    metadata and classifies as the generic value sensor.
    """

    # Sensor side. ``sensor`` is a fixed kind; ``analog`` selects the
    # interpretacja-keyed lin_wej map; ``numeric`` is a generic bit32
    # measurement. A type with none of these is not a sensor.
    sensor: SensorKind | None = None
    analog: bool = False
    numeric: bool = False
    # Input side: the binary/flag kind, if this type is an input.
    input: InputKind | None = None
    # Output side: the controllable kind, if this type accepts commands.
    output: OutputKind | None = None
    # Thermostat side: the temperature-controller kind.
    thermostat: ThermostatKind | None = None
    # Raw ``ampio/from/<mac>/state/<prefix>/<ch>`` bridge prefix. Only known
    # prefixes are set; an input without one (symulacja) falls back to the
    # per-object topic.
    channel_prefix: str | None = None
    # System objects (presence simulation / detection) live outside the
    # room/group hierarchy; the M-SERV always exposes them, so they read as
    # visible even with an empty leafId and no group membership.
    system: bool = False


TYPE_PROFILES: dict[str, TypeProfile] = {
    "temp": TypeProfile(
        sensor=SensorKind("temperature", "Temperature", "°C", "temperature")
    ),
    "lin_wej": TypeProfile(analog=True),
    "bit32": TypeProfile(numeric=True),
    "przekaznik": TypeProfile(output=OutputKind("relay", "Relay")),
    "rgbw": TypeProfile(
        output=OutputKind("rgbw", "RGBW light", color=True, switchable=False)
    ),
    "led": TypeProfile(output=OutputKind("dimmer", "Dimmer", dimmable=True)),
    "roleta": TypeProfile(output=OutputKind("cover", "Cover", cover=True)),
    "roleta_procenty": TypeProfile(
        output=OutputKind("cover_position", "Cover", cover=True, position=True)
    ),
    "roleta_lamelki": TypeProfile(
        output=OutputKind("cover_tilt", "Blind", cover=True, position=True, tilt=True)
    ),
    "reg": TypeProfile(thermostat=ThermostatKind("thermostat", "Thermostat")),
    "bit8": TypeProfile(numeric=True),
    "flaga": TypeProfile(input=InputKind("flaga", "Flag", None), channel_prefix="f"),
    "detekcja": TypeProfile(
        input=InputKind("detekcja", "Detection", "motion"),
        channel_prefix="i",
        system=True,
    ),
    "symulacja": TypeProfile(
        input=InputKind("symulacja", "Simulation", None), system=True
    ),
}


def _kind_keys() -> tuple[
    frozenset[str], frozenset[str], frozenset[str], frozenset[str]
]:
    sensor: set[str] = {_GENERIC_SENSOR.key}
    inputs: set[str] = set()
    output: set[str] = set()
    thermostat: set[str] = set()
    for profile in TYPE_PROFILES.values():
        if profile.sensor is not None:
            sensor.add(profile.sensor.key)
        if profile.analog:
            sensor.update(kind.key for kind in _LIN_WEJ_BY_INTERP.values())
        if profile.input is not None:
            inputs.add(profile.input.key)
        if profile.output is not None:
            output.add(profile.output.key)
        if profile.thermostat is not None:
            thermostat.add(profile.thermostat.key)
    return (
        frozenset(sensor),
        frozenset(inputs),
        frozenset(output),
        frozenset(thermostat),
    )


# The complete static `kind.key` vocabulary, derived from the tables above
# at import time so a new row is part of it with no second edit. A consumer
# mapping `kind.key` to its own entity descriptions should assert in its CI
# that every key here is either mapped or deliberately excluded - a library
# upgrade that adds a kind then fails a test instead of silently dropping
# entities.
SENSOR_KIND_KEYS, INPUT_KIND_KEYS, OUTPUT_KIND_KEYS, THERMOSTAT_KIND_KEYS = _kind_keys()

# The two open families: keys minted with the object's `interpretacja`
# embedded (`analog_<n>` for a lin_wej measurement the map does not know,
# `value_<n>` for the numeric bit8/bit32 channels), so they cannot be
# enumerated. A consumer treats each prefix as one mapping decision.
OPEN_SENSOR_KEY_PREFIXES: tuple[str, ...] = ("analog_", "value_")


def classify(typ: str | None, interpretacja: int | None) -> ObjectKind:
    """Classify a DB object into the one kind it is.

    ``typ`` is ``typ_komponentu``; ``interpretacja`` selects the lin_wej
    measurement. A ``typ`` with no table entry (unknown, or no metadata yet)
    is the generic value-only sensor, so such an object still surfaces.
    """
    profile = TYPE_PROFILES.get(typ) if typ is not None else None
    if profile is None:
        return _GENERIC_SENSOR
    if profile.analog:
        if interpretacja in _LIN_WEJ_BY_INTERP:
            return _LIN_WEJ_BY_INTERP[interpretacja]
        return SensorKind(f"analog_{interpretacja}", "Analog input", None, None)
    if profile.numeric:
        return SensorKind(f"value_{interpretacja}", "Measurement", None, None)
    return (
        profile.sensor
        or profile.input
        or profile.output
        or profile.thermostat
        or _GENERIC_SENSOR
    )


def is_system_type(typ: str | None) -> bool:
    """Whether ``typ`` is a system component the M-SERV always exposes."""
    profile = TYPE_PROFILES.get(typ) if typ is not None else None
    return profile.system if profile is not None else False


def input_channel_prefix(typ: str | None) -> str | None:
    """Raw-channel bridge prefix for ``typ``, or None if it bridges no channel."""
    profile = TYPE_PROFILES.get(typ) if typ is not None else None
    return profile.channel_prefix if profile is not None else None
