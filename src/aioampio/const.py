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
from typing import Literal, get_args

DETAILS_REQUEST_PAYLOAD = "devicesDetails"
DEVICES_REQUEST_PAYLOAD = "devices"


def ob_state_wildcard(user: str) -> str:
    """Wildcard for all object state topics for an account."""
    return f"ampio/fromDB/{user}/ob/+/state"


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

_VALID_DEVICE_CLASSES = frozenset(get_args(DeviceClass))
_VALID_STATE_CLASSES = frozenset(get_args(StateClass))


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

    def __post_init__(self) -> None:
        """Reject device/state class strings that are not in the typed set."""
        if (
            self.device_class is not None
            and self.device_class not in _VALID_DEVICE_CLASSES
        ):
            raise ValueError(f"Unknown SensorKind device_class: {self.device_class!r}")
        if (
            self.state_class is not None
            and self.state_class not in _VALID_STATE_CLASSES
        ):
            raise ValueError(f"Unknown SensorKind state_class: {self.state_class!r}")


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
