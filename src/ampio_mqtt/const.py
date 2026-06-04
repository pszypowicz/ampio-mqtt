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

# --- Endpoint table --------------------------------------------------------
#
# Every request/response endpoint the M-SERV exposes is one row here, and that
# row is the single source of truth: the client derives its subscriptions,
# topic-to-handler routing, discovery-completion signals, and retained payloads
# from this table. Adding an endpoint is one row, not edits in four places.
#
# A request publishes ``req_payload`` (a keyword, or "" for the dedicated
# ``states``/``info`` surfaces) to ``ampio/control/<user>/<req_surface>``; the
# reply lands on ``ampio/fromDB/<user>/<resp_surface>/<resp_leaf>``.


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One M-SERV request/response endpoint."""

    name: str
    req_surface: str  # control sub-topic: "config" | "states" | "info" | "data"
    req_payload: str  # request keyword, or "" for the states/info surfaces
    resp_surface: str  # fromDB sub-topic: "config" | "data"
    resp_leaf: str  # final response-topic segment
    # Part of the initial-discovery set awaited by start() /
    # wait_for_initial_discovery(). The rooms/locations endpoints are on-demand.
    initial: bool = False


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("details", "config", "devicesDetails", "config", "devicesDetails", True),
    Endpoint("devices", "config", "devices", "config", "devices", True),
    Endpoint("states", "states", "", "data", "states", True),
    Endpoint("info", "info", "", "data", "info", True),
    Endpoint("groups", "data", "groups", "data", "groups"),
    Endpoint("group_devices", "data", "group_devices", "data", "group_devices"),
    Endpoint("locations", "config", "locations", "config", "locations"),
)

ENDPOINT_BY_NAME: dict[str, Endpoint] = {ep.name: ep for ep in ENDPOINTS}


def request_topic(ep: Endpoint, user: str) -> str:
    """Control topic an endpoint's request keyword is published to."""
    return f"ampio/control/{user}/{ep.req_surface}"


def response_topic(ep: Endpoint, user: str) -> str:
    """fromDB topic an endpoint's reply arrives on."""
    return f"ampio/fromDB/{user}/{ep.resp_surface}/{ep.resp_leaf}"


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

# Generic value-only sensor for an object with no usable metadata (e.g. a
# restricted account that never receives `devicesDetails`). The value may be
# non-numeric, so it claims neither a state class nor a precision - both would
# make Home Assistant reject a text value.
_GENERIC_SENSOR = SensorKind(
    "value", "Value", None, None, state_class=None, precision=None
)


@dataclass(frozen=True, slots=True)
class TypeProfile:
    """Everything the library derives from one ``typ_komponentu``.

    One row per known component type, replacing the former overlapping
    SENSOR_TYPES / NON_SENSOR_TYPES / INPUT_TYPES / SYSTEM_TYPES sets and the
    channel-prefix map. A type absent from the table is unknown metadata and
    classifies as the generic value sensor.
    """

    # Sensor side - at most one applies. ``sensor`` is a fixed kind; ``analog``
    # selects the interpretacja-keyed lin_wej map; ``numeric`` is a generic
    # bit32 measurement. A type with none of these is not a sensor.
    sensor: SensorKind | None = None
    analog: bool = False
    numeric: bool = False
    # Input side: the binary/flag kind, if this type is an input.
    input: InputKind | None = None
    # Raw ``ampio/from/<mac>/state/<prefix>/<ch>`` bridge prefix. Only verified
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
    "przekaznik": TypeProfile(),
    "rgbw": TypeProfile(),
    "led": TypeProfile(),
    "roleta_procenty": TypeProfile(),
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


def classify(
    typ: str | None, interpretacja: int | None
) -> tuple[SensorKind | None, InputKind | None]:
    """Classify a DB object into ``(sensor_kind, input_kind)``; either may be None.

    ``typ`` is ``typ_komponentu``; ``interpretacja`` selects the lin_wej
    measurement. A ``typ`` with no table entry (unknown or no metadata) returns
    the generic value-only sensor so restricted accounts still surface sensors.
    """
    profile = TYPE_PROFILES.get(typ) if typ is not None else None
    if profile is None:
        return _GENERIC_SENSOR, None
    sensor = profile.sensor
    if profile.analog:
        if interpretacja in _LIN_WEJ_BY_INTERP:
            sensor = _LIN_WEJ_BY_INTERP[interpretacja]
        else:
            sensor = SensorKind(f"analog_{interpretacja}", "Analog input", None, None)
    elif profile.numeric:
        sensor = SensorKind(f"value_{interpretacja}", "Measurement", None, None)
    return sensor, profile.input


def is_system_type(typ: str | None) -> bool:
    """Whether ``typ`` is a system component the M-SERV always exposes."""
    profile = TYPE_PROFILES.get(typ) if typ is not None else None
    return profile.system if profile is not None else False


def input_channel_prefix(typ: str | None) -> str | None:
    """Raw-channel bridge prefix for ``typ``, or None if it bridges no channel."""
    profile = TYPE_PROFILES.get(typ) if typ is not None else None
    return profile.channel_prefix if profile is not None else None
