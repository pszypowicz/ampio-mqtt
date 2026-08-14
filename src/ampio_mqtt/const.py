"""Constants and object classification for the Ampio DB-object MQTT protocol.

Protocol: MQTT topics are namespaced by the connecting account:
  state:     ampio/fromDB/<user>/ob/<id>/state   -> {"state","desc","on"}
  objects:   publish ampio/control/<user>/config = "devicesDetails"
             -> ampio/fromDB/<user>/config/devicesDetails = {"Status":0,"List":[...]}
  modules:   publish ampio/control/<user>/config = "devices"
             -> ampio/fromDB/<user>/config/devices = {"List":[{id,mac,
                nazwa_urzadzenia,typ_urzadzenia,wersja_softu,...}]}

The same ampio/control/<user>/config topic carries every discovery request; the
payload keyword selects what the server publishes back. The `config` surface
answers only for administrator accounts. Non-admin accounts are served the
app-sync `data` surface instead: `data/devices` (objects, grant-filtered to
what the account can see in the app; same row shape as `devicesDetails` minus
`params`/`stan_json`) and `data/params_devices` (the `params` bitfields,
unfiltered). See `AccessTier` and the discovery groups below.

This module is Home Assistant agnostic; device/state class strings match Home
Assistant's SensorDeviceClass / SensorStateClass enum values so consumers can
pass them through unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
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
    # App-sync object catalogue. Same wire keyword as the module list above but
    # on the `data` surface, and a different payload: DB objects (the
    # `devicesDetails` row shape minus `params`/`stan_json`), filtered to the
    # objects the account was granted in the Ampio app. Unlike the `config`
    # surface it answers for every account, so it is the discovery fallback
    # for non-admin accounts.
    Endpoint("data_devices", "data", "devices", "data", "devices", True),
    # Per-object `params` bitfields for the app-sync catalogue. NOT
    # grant-filtered: every account receives the full table, which is what
    # lets a restricted account apply the hidden-flag visibility rule.
    Endpoint(
        "params_devices", "data", "params_devices", "data", "params_devices", True
    ),
    Endpoint("groups", "data", "groups", "data", "groups"),
    Endpoint("group_devices", "data", "group_devices", "data", "group_devices"),
    Endpoint("locations", "config", "locations", "config", "locations"),
    Endpoint("scenes", "data", "scenes", "data", "scenes"),
)

ENDPOINT_BY_NAME: dict[str, Endpoint] = {ep.name: ep for ep in ENDPOINTS}


class AccessTier(Enum):
    """Account tier, detected from which discovery surface answers.

    The M-SERV gates the ``config`` surface (and the raw ``ampio/from/#``
    channel tree) on the account's administrator bit; the per-user app
    permissions do not affect it. A non-admin account, however permissioned,
    is served only the app-sync ``data`` surface.
    """

    UNKNOWN = "unknown"  # neither surface has answered yet
    ADMIN = "admin"  # the config surface answered: full catalogue + modules
    RESTRICTED = "restricted"  # only the data surface answered: app-sync view


# Initial-discovery endpoint groups by tier. Discovery is complete when the
# common pair plus either tier's catalogue pair have latched; which pair
# answered determines `AmpioClient.access_tier`.
DISCOVERY_COMMON: tuple[str, ...] = ("states", "info")
DISCOVERY_ADMIN: tuple[str, ...] = ("details", "devices")
DISCOVERY_FALLBACK: tuple[str, ...] = ("data_devices", "params_devices")


# --- Commands --------------------------------------------------------------
#
# Writes go to one control topic per account as plain text:
# ``/api/set/<object_id>/<verb>[/<arg>...]``. The verb vocabulary is the
# M-SERV's own HTTP API, re-exposed over MQTT; see docs/protocol.md for the
# table and which verbs are verified live.
#
# The per-user grant bounds writes as it bounds reads: a command for an object
# outside the account's grant is dropped with no effect and no reply.


def command_topic(user: str) -> str:
    """Control topic that carries object commands for an account."""
    return f"ampio/control/{user}/api"


def command_payload(object_id: int, verb: str, args: Sequence[object] = ()) -> str:
    """Build an ``/api/set`` command payload."""
    return f"/api/set/{object_id}/{verb}" + "".join(f"/{a}" for a in args)


def event_payload(event_number: int) -> str:
    """Build the payload that raises a bus event."""
    return f"/api/setEvent/{event_number}"


def scene_payload(scene_id: int, verb: str) -> str:
    """Build a scene command payload; ``verb`` is run, off, or undo."""
    return f"/api/{verb}/scene/{scene_id}"


# `setRollerPos` takes a position and a lamella angle. 101 on either axis means
# "leave this one where it is", so one command can move either axis alone or
# both together.
KEEP_POSITION = 101


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

# Modules periodically broadcast a diagnostics frame on `ampio/from/<MAC>/b/4F`
# carrying their CAN supply voltage and, on the modules that measure it, their
# own temperature. Like the rest of the raw tree this is administrator-only.
RAW_DIAGNOSTICS_WILDCARD = "ampio/from/+/b/4F"

# Bus events are logical signals (1-65535) that Ampio logic raises and reacts
# to - a wall-panel press can raise one, and a scenario can be bound to one.
# Receiving them rides the administrator-only raw tree. Raising one goes to the
# command surface, works on both tiers, and is bounded by nothing - not object
# grants, and not the per-event rights the app displays.
RAW_EVENT_WILDCARD = "ampio/from/+/event"


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


@dataclass(frozen=True, slots=True)
class OutputKind:
    """Neutral description of a controllable output object.

    The flags say which command verbs the object answers, so a consumer can
    pick a platform and feature set without a `typ_komponentu` table of its
    own. Every output answers ``turnOn`` / ``turnOff`` / ``switch``.
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
    # state payload. `setRollerPos` ignores the KEEP_POSITION sentinel on
    # these, so the lamella argument must carry a real angle.
    tilt: bool = False


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


# What an object is. Exactly one of the three applies - a component type is a
# measurement, a boolean input, or something controllable, never two - so the
# kinds are alternatives rather than a set of optional slots.
ObjectKind = SensorKind | InputKind | OutputKind


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
    "rgbw": TypeProfile(output=OutputKind("rgbw", "RGBW light", color=True)),
    "led": TypeProfile(output=OutputKind("dimmer", "Dimmer", dimmable=True)),
    "roleta": TypeProfile(output=OutputKind("cover", "Cover", cover=True)),
    "roleta_procenty": TypeProfile(
        output=OutputKind("cover_position", "Cover", cover=True, position=True)
    ),
    "roleta_lamelki": TypeProfile(
        output=OutputKind("cover_tilt", "Blind", cover=True, position=True, tilt=True)
    ),
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
    return profile.sensor or profile.input or profile.output or _GENERIC_SENSOR


def is_system_type(typ: str | None) -> bool:
    """Whether ``typ`` is a system component the M-SERV always exposes."""
    profile = TYPE_PROFILES.get(typ) if typ is not None else None
    return profile.system if profile is not None else False


def input_channel_prefix(typ: str | None) -> str | None:
    """Raw-channel bridge prefix for ``typ``, or None if it bridges no channel."""
    profile = TYPE_PROFILES.get(typ) if typ is not None else None
    return profile.channel_prefix if profile is not None else None
