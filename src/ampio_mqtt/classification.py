"""Object classification for the Ampio DB-object protocol.

One `TypeProfile` row per known ``typ_komponentu`` drives everything the
library derives from a component type: its kind (sensor, input, output or
thermostat) and an input's raw-channel bridge prefix. This module is Home
Assistant agnostic; device/state class strings match Home Assistant's
SensorDeviceClass / SensorStateClass enum values so consumers can pass them
through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
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


@dataclass(frozen=True, slots=True)
class InputKind:
    """Neutral description of a binary / flag-shaped input object.

    ``switchable`` carries the same claim it does on `OutputKind`, so a
    consumer can partition writable inputs from read-only ones with one
    predicate across both classes.
    """

    key: str
    name: str
    # The `turnOn` / `turnOff` / `switch` verb family, over `/api`. True only
    # for `flaga`. A `wej` is a physical input the module scans for itself:
    # the M-SERV drops all three verbs for it on both account tiers, with no
    # effect and no reply.
    switchable: bool = False
    # The inclusive range `setValue` holds, for a flag with a value axis;
    # None for a flag that carries no value. The M-SERV truncates an
    # out-of-range write to the field width instead of refusing it, so the
    # range is what a caller must respect, not a hint.
    value_range: tuple[int, int] | None = None
    # Whether a `setValue` time argument runs a timed pulse. True only for
    # `flaga`. Both analog flags take the timed form, set the value and
    # latch: the revert never arrives, at any time argument. `wej` takes no
    # value verb at all.
    pulsable: bool = False


@dataclass(frozen=True, slots=True)
class OutputKind:
    """Neutral description of a controllable output object.

    The flags say which command verbs the object answers, so a consumer can
    pick a platform and feature set without a `typ_komponentu` table of its
    own. ``switchable`` and ``toggleable`` split the switch-verb family in
    two, because ``ledww`` answers ``switch`` while ignoring ``turnOn`` and
    ``turnOff``. One flag cannot state that, so they are separate fields.
    """

    key: str
    name: str
    # 0-255 level via `setValue`.
    dimmable: bool = False
    # Four RGBW channels via `setColors`.
    color: bool = False
    # A power axis and a color-temperature axis via `setWW` / `setWWPower`,
    # packed into one u16 state value (`AmpioObject.cct`).
    color_temp: bool = False
    # `open` / `close` travel commands.
    cover: bool = False
    # Position axis of `setRollerPos`.
    position: bool = False
    # Lamella axis of `setRollerPos`, and a `lammel` field in the object's
    # state payload; `KEEP_POSITION` in _protocol.py is the
    # leave-this-axis-alone sentinel both axes share.
    tilt: bool = False
    # The `turnOn` / `turnOff` verbs. False for `rgbw`, which the M-SERV
    # drives through `setColors` alone, and for `ledww`, whose power axis
    # moves through `setWWPower` - both of which `AmpioClient.turn_off`
    # relies on to emulate off.
    switchable: bool = True
    # The `switch` verb alone. False only for `rgbw`. A `ledww` answers it
    # as a toggle between two remembered states.
    toggleable: bool = True
    # Whether a `setValue` time argument runs a timed pulse. True for the
    # relay and the dimmer. `rgbw` and the covers answer no `setValue` at
    # all, and a `ledww` takes the timed form, sets the power and zeroes
    # the color temperature with no revert (docs/commands.md).
    pulsable: bool = False


# The two halves of an alarm partition, keyed by the leaf sub-function
# (`AmpioObject.address.sub_sf_id`). The catalogue row cannot tell them
# apart: both carry the same `typ_komponentu`, `funkcja` and
# `interpretacja`, and only the leaf's sub-function differs. The Designer
# names the special function after the alarm panel family, and marks both
# halves read-only. Neither takes a device class: "alarmed" also reads 1
# through the panel's exit delay, so it is not a safety indicator on its
# own (docs/commands.md).
_ALARM_BY_SUB_SF: dict[int, InputKind] = {
    3: InputKind("alarm_armed", "Alarm armed"),
    4: InputKind("alarm_alarmed", "Alarm triggered"),
}

# The alarm family when the sub-function names neither half.
# `typ_komponentu` holds the family and the leaf's sub-function refines the
# name, so an unlisted sub-function keeps the base kind. No device class,
# for the reason the two halves take none.
_BASE_ALARM = InputKind("alarm", "Alarm")

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

# Generic value-only sensor for a catalogue row with no usable metadata
# (a `typ_komponentu` missing from TYPE_PROFILES). The value may be
# non-numeric, so it claims neither a state class nor a precision - both
# would make Home Assistant reject a text value.
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
    is surfaced as :attr:`AmpioObject.thermostat`.
    """

    key: str
    name: str


# What an object is. Exactly one applies - a component type is a measurement,
# a boolean input, something controllable, or a thermostat, never two - so
# the kinds are alternatives rather than a set of optional slots.
ObjectKind = SensorKind | InputKind | OutputKind | ThermostatKind


class _Selector(Enum):
    """The kind families a profile carries in place of a fixed kind
    instance, because one ``typ_komponentu`` covers several kinds. A second
    wire field picks the member."""

    ANALOG = auto()  # the interpretacja-keyed lin_wej map, open below it
    NUMERIC = auto()  # generic value_<interpretacja> measurement (integer slots)
    ALARM = auto()  # the sub_sf_id-keyed alarm partition halves


@dataclass(frozen=True, slots=True)
class TypeProfile:
    """Everything the library derives from one ``typ_komponentu``.

    One row per known component type; a type absent from the table is
    unknown metadata and classifies as the generic value sensor. ``kind``
    is the one kind the type is - a fixed instance, or a `_Selector` for
    the families keyed by ``interpretacja`` or the leaf sub-function.
    """

    kind: ObjectKind | _Selector
    # Raw ``ampio/from/<mac>/state/<prefix>/<ch>`` bridge prefix, set only
    # for a prefix the admin client subscribes. An input without one updates
    # on the per-object topic.
    channel_prefix: str | None = None


TYPE_PROFILES: dict[str, TypeProfile] = {
    "temp": TypeProfile(SensorKind("temperature", "Temperature", "°C", "temperature")),
    "lin_wej": TypeProfile(_Selector.ANALOG),
    "bit32": TypeProfile(_Selector.NUMERIC),
    "przekaznik": TypeProfile(OutputKind("relay", "Relay", pulsable=True)),
    "rgbw": TypeProfile(
        OutputKind("rgbw", "RGBW light", color=True, switchable=False, toggleable=False)
    ),
    "led": TypeProfile(OutputKind("dimmer", "Dimmer", dimmable=True, pulsable=True)),
    # Warm/cold white.
    "ledww": TypeProfile(
        OutputKind(
            "cct", "CCT light", color_temp=True, switchable=False, toggleable=True
        )
    ),
    "roleta": TypeProfile(OutputKind("cover", "Cover", cover=True)),
    "roleta_procenty": TypeProfile(
        OutputKind("cover_position", "Cover", cover=True, position=True)
    ),
    "roleta_lamelki": TypeProfile(
        OutputKind("cover_tilt", "Blind", cover=True, position=True, tilt=True)
    ),
    "reg": TypeProfile(ThermostatKind("thermostat", "Thermostat")),
    # The four integer sensor slots an M-CON-485 lands a Modbus reading in:
    # Designer's `bit 8`, `bit 16`, `sbit 16[+/-]`, and `bit 32` subtypes.
    "bit8": TypeProfile(_Selector.NUMERIC),
    "bit16": TypeProfile(_Selector.NUMERIC),
    "sbit16": TypeProfile(_Selector.NUMERIC),
    "flaga": TypeProfile(
        InputKind("flaga", "Flag", switchable=True, pulsable=True),
        channel_prefix="f",
    ),
    # The analog flags, the module's own u8 and signed-i16 variables. Both
    # answer `setValue` and both wrap silently past their field width.
    "flaga_liniowa": TypeProfile(
        InputKind("flaga_liniowa", "Analog flag", value_range=(0, 255)),
    ),
    "flaga_liniowa16": TypeProfile(
        InputKind(
            "flaga_liniowa16", "Analog flag (16-bit)", value_range=(-32768, 32767)
        ),
    ),
    "satel_alarm": TypeProfile(_Selector.ALARM),
    # The per-channel physical-input object (a wall button wired to a module
    # terminal). Same 255/0 payload as flags on the per-object topic; the
    # raw mirror rides the digital-input prefix (#117).
    "wej": TypeProfile(InputKind("wej", "Input"), channel_prefix="i"),
}


def _kind_keys() -> tuple[
    frozenset[str], frozenset[str], frozenset[str], frozenset[str]
]:
    sensor: set[str] = {_GENERIC_SENSOR.key}
    inputs: set[str] = set()
    output: set[str] = set()
    thermostat: set[str] = set()
    for profile in TYPE_PROFILES.values():
        match profile.kind:
            case SensorKind() as kind:
                sensor.add(kind.key)
            case _Selector.ANALOG:
                sensor.update(kind.key for kind in _LIN_WEJ_BY_INTERP.values())
            case _Selector.NUMERIC:
                pass  # the open value_<interpretacja> family
            case _Selector.ALARM:
                inputs.add(_BASE_ALARM.key)
                inputs.update(kind.key for kind in _ALARM_BY_SUB_SF.values())
            case InputKind() as kind:
                inputs.add(kind.key)
            case OutputKind() as kind:
                output.add(kind.key)
            case ThermostatKind() as kind:
                thermostat.add(kind.key)
    return (
        frozenset(sensor),
        frozenset(inputs),
        frozenset(output),
        frozenset(thermostat),
    )


# The complete static `kind.key` vocabulary, derived from the tables above
# at import time so a new row is part of it with no second edit. The
# consumer-CI contract built on these lives in docs/classification.md.
SENSOR_KIND_KEYS, INPUT_KIND_KEYS, OUTPUT_KIND_KEYS, THERMOSTAT_KIND_KEYS = _kind_keys()

# The two open families: keys minted with the object's `interpretacja`
# embedded (`analog_<n>` for a lin_wej measurement the map does not know,
# `value_<n>` for the numeric bit8/bit16/sbit16/bit32 slots), so they cannot be
# enumerated. A consumer treats each prefix as one mapping decision.
SENSOR_KIND_KEY_PREFIXES: tuple[str, ...] = ("analog_", "value_")


def classify(
    typ_komponentu: str | None,
    interpretacja: int | None,
    sub_sf_id: int = 0,
) -> ObjectKind:
    """Classify a DB object into the one kind it is.

    ``interpretacja`` selects the lin_wej measurement and names the numeric
    ``value_<n>`` key. A ``typ_komponentu`` with no table entry (unknown,
    or no metadata yet) is the generic value-only sensor, so such an
    object still surfaces. ``sub_sf_id`` is
    the leaf sub-function, 0 for a single-role class, which refines
    ``satel_alarm`` alone.
    """
    profile = TYPE_PROFILES.get(typ_komponentu) if typ_komponentu is not None else None
    if profile is None:
        return _GENERIC_SENSOR
    match profile.kind:
        case _Selector.ANALOG:
            if interpretacja in _LIN_WEJ_BY_INTERP:
                return _LIN_WEJ_BY_INTERP[interpretacja]
            return SensorKind(f"analog_{interpretacja}", "Analog input", None, None)
        case _Selector.NUMERIC:
            return SensorKind(f"value_{interpretacja}", "Measurement", None, None)
        case _Selector.ALARM:
            return _ALARM_BY_SUB_SF.get(sub_sf_id, _BASE_ALARM)
        case kind:
            return kind


def input_channel_prefix(typ_komponentu: str | None) -> str | None:
    """Raw-channel bridge prefix for an input ``typ_komponentu``, or None.

    Outputs pick their prefix in the store.
    """
    profile = TYPE_PROFILES.get(typ_komponentu) if typ_komponentu is not None else None
    return profile.channel_prefix if profile is not None else None
