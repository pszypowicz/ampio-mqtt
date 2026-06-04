"""Constants and object classification for the Ampio DB-object MQTT protocol.

Protocol (validated live): MQTT topics are namespaced by the connecting account:
  state:     ampio/fromDB/<user>/ob/<id>/state   -> {"state","desc","on"}
  objects:   publish ampio/control/<user>/config = "devicesDetails"
             -> ampio/fromDB/<user>/config/devicesDetails = {"Status":0,"List":[...]}
  modules:   publish ampio/control/<user>/config = "devices"
             -> ampio/fromDB/<user>/config/devices = {"List":[{id,mac,
                nazwa_urzadzenia,typ_urzadzenia,wersja_softu,...}]}

The same ampio/control/<user>/config topic carries every discovery request; the
payload keyword selects what the server publishes back.

This module is Home Assistant agnostic; device/state class strings match Home
Assistant's SensorDeviceClass / SensorStateClass enum values so consumers can
pass them through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DETAILS_REQUEST_PAYLOAD = "devicesDetails"
DEVICES_REQUEST_PAYLOAD = "devices"
GROUPS_REQUEST_PAYLOAD = "groups"
GROUP_DEVICES_REQUEST_PAYLOAD = "group_devices"
LOCATIONS_REQUEST_PAYLOAD = "locations"


def ob_state_wildcard(user: str) -> str:
    """Wildcard for all object state topics for an account."""
    return f"ampio/fromDB/{user}/ob/+/state"


# Raw, module-scoped channel topics carry decoded CAN state per channel index
# and are NOT namespaced by user (the `ampio/from/<MAC>/...` tree is global).
# We subscribe only to the two input prefixes - `f` (flags) and `i` (digital
# inputs) - because they publish on-change and are the low-latency source for
# input objects. The high-rate prefixes (`a`/`t`/`rgbw`/`o`) are intentionally
# excluded; those object types already arrive on the per-object topic.
RAW_INPUT_WILDCARDS = ("ampio/from/+/state/f/+", "ampio/from/+/state/i/+")


def config_request_topic(user: str) -> str:
    """Shared topic for publishing any discovery request keyword."""
    return f"ampio/control/{user}/config"


def details_response_topic(user: str) -> str:
    """Topic carrying the devicesDetails (object list) response."""
    return f"ampio/fromDB/{user}/config/devicesDetails"


def devices_response_topic(user: str) -> str:
    """Topic carrying the devices (physical module list) response."""
    return f"ampio/fromDB/{user}/config/devices"


def states_request_topic(user: str) -> str:
    """Topic used to request a snapshot of all object states (empty payload)."""
    return f"ampio/control/{user}/states"


def states_response_topic(user: str) -> str:
    """Topic carrying the bulk object-states snapshot response."""
    return f"ampio/fromDB/{user}/data/states"


def info_request_topic(user: str) -> str:
    """Topic used to request server info (empty payload)."""
    return f"ampio/control/{user}/info"


def info_response_topic(user: str) -> str:
    """Topic carrying the server info response."""
    return f"ampio/fromDB/{user}/data/info"


def data_request_topic(user: str) -> str:
    """Shared topic for publishing 'data' request keywords (groups, group_devices)."""
    return f"ampio/control/{user}/data"


def groups_response_topic(user: str) -> str:
    """Topic carrying the rooms/groups list response."""
    return f"ampio/fromDB/{user}/data/groups"


def group_devices_response_topic(user: str) -> str:
    """Topic carrying the object-to-group join response."""
    return f"ampio/fromDB/{user}/data/group_devices"


def locations_response_topic(user: str) -> str:
    """Topic carrying the locations (Designer location-marker) table response.

    The table is published on the *config* surface, not the *data* surface
    like the rooms join is - it lives next to ``devicesDetails`` /
    ``devices``. Triggered by publishing ``LOCATIONS_REQUEST_PAYLOAD`` on
    ``config_request_topic(user)``.
    """
    return f"ampio/fromDB/{user}/config/locations"


# --- Sensor classification -------------------------------------------------

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


# lin_wej (analog input) measurement kind, keyed by `interpretacja`.
# Matches the M-SENS channel map confirmed live (4=lux, 5=IAQ, 7=CO2).
_LIN_WEJ_BY_INTERP: dict[int, SensorKind] = {
    1: SensorKind("humidity", "Humidity", "%", "humidity"),
    2: SensorKind("pressure_abs", "Pressure (absolute)", "hPa", "atmospheric_pressure"),
    3: SensorKind("loudness", "Loudness", "dB", "sound_pressure"),
    4: SensorKind("illuminance", "Illuminance", "lx", "illuminance", precision=0),
    5: SensorKind("iaq", "Air quality index", None, "aqi", precision=0),
    6: SensorKind("pressure_rel", "Pressure (relative)", "hPa", "pressure"),
    7: SensorKind("co2", "CO2", "ppm", "carbon_dioxide", precision=0),
}

# typ_komponentu values that the sensor platform handles.
SENSOR_TYPES = frozenset({"temp", "lin_wej", "bit32"})
# Known non-sensor types (handled by future platforms) - never sensors.
NON_SENSOR_TYPES = frozenset(
    {"przekaznik", "rgbw", "led", "roleta_procenty", "flaga", "detekcja", "symulacja"}
)


def classify_object(typ: str | None, interpretacja: int | None) -> SensorKind | None:
    """Classify a DB object into a SensorKind, or None if it is not a sensor.

    `typ` is `typ_komponentu`; `interpretacja` selects the lin_wej measurement.
    Unknown `typ` (e.g. no metadata available) returns a generic measurement so
    restricted accounts still get value-only sensors.
    """
    if typ == "temp":
        return SensorKind("temperature", "Temperature", "°C", "temperature")
    if typ == "lin_wej":
        if interpretacja in _LIN_WEJ_BY_INTERP:
            return _LIN_WEJ_BY_INTERP[interpretacja]
        return SensorKind(f"analog_{interpretacja}", "Analog input", None, None)
    if typ == "bit32":
        return SensorKind(f"value_{interpretacja}", "Measurement", None, None)
    if typ in NON_SENSOR_TYPES:
        return None
    # Unknown / no metadata -> generic value-only sensor (restricted accounts).
    # May be non-numeric, so claim neither a state class nor a precision (both
    # would make Home Assistant reject a text value).
    return SensorKind("value", "Value", None, None, state_class=None, precision=None)


# --- Input classification --------------------------------------------------

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


# typ_komponentu values handled by the input (binary_sensor) platform. These
# also live in NON_SENSOR_TYPES - they are not sensors, but they are inputs.
INPUT_TYPES = frozenset({"flaga", "detekcja", "symulacja"})

# typ_komponentu values that are SYSTEM objects: the M-SERV always exposes
# them regardless of grouping, and Designer's own "visible objects" query
# treats them as visible even when they have no `powiazane` entry. Used by
# `AmpioObject.is_system` / `visible`. Kept narrow on purpose - `flaga` is
# an input but not a system object (it can be ungrouped without being one).
SYSTEM_TYPES = frozenset({"symulacja", "detekcja"})

# typ_komponentu -> raw channel-topic prefix, for the channel bridge. Only
# verified prefixes are bridged; symulacja classifies as an input but is left
# off until its prefix is confirmed (it falls back to the per-object topic).
_INPUT_CHANNEL_PREFIX = {
    "flaga": "f",  # confirmed live
    "detekcja": "i",  # digital input, confirmed live
}


def classify_input(typ: str | None, interpretacja: int | None) -> InputKind | None:
    """Classify a DB object into an InputKind, or None if it is not an input.

    ``flaga`` -> generic boolean (``device_class=None``); a persistent 0/1 logic
    flag the consumer may surface as a binary_sensor or a switch.
    ``detekcja`` -> ``motion``.
    ``symulacja`` -> generic boolean (the M-SERV presence-simulation flag).

    ``interpretacja`` is accepted for parity with :func:`classify_object` and
    possible future per-interpretation mapping; it is unused today.
    """
    if typ == "detekcja":
        return InputKind("detekcja", "Detection", "motion")
    if typ == "flaga":
        return InputKind("flaga", "Flag", None)
    if typ == "symulacja":
        return InputKind("symulacja", "Simulation", None)
    return None
