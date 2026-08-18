"""Everything wire-shaped for the Ampio DB-object MQTT protocol.

One module owns both directions: the endpoint table with its topic and
command builders (what the client says), and the pure parsers with the
Router (what the wire says back). No I/O and no state mutation - the
`AmpioStore` applies the typed results to its state.

Topics are namespaced by the connecting account:
  state:     ampio/fromDB/<user>/ob/<id>/state   -> {"state","desc","on"}
  objects:   publish ampio/control/<user>/config = "devicesDetails"
             -> ampio/fromDB/<user>/config/devicesDetails = {"Status":0,"List":[...]}
  modules:   publish ampio/control/<user>/config = "devices"
             -> ampio/fromDB/<user>/config/devices = {"List":[...]}

The same ampio/control/<user>/config topic carries every discovery request;
the payload keyword selects what the server publishes back. The `config`
surface answers only for administrator accounts; non-admin accounts are
served the app-sync `data` surface instead - see :class:`AccessTier` and
the endpoint table below.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .device_types import module_model
from .events import BusEvent
from .models import AccessTier, AmpioModule, AmpioScene, AmpioServerInfo


@dataclass(slots=True)
class ObjectMetadata:
    """Per-object metadata from a `devicesDetails` payload."""

    id: int
    device_id: int | None
    typ_komponentu: str | None
    name: str | None
    interpretacja: int | None
    funkcja: int | None  # physical channel index within the module
    leaf_id: str  # `leafId`; empty for ghost rows and for system objects
    # `params` bitfield; bit 4 = hidden/stub, bit 37 = matter-exposed. None
    # when the reply carried no such column, which the app-sync catalogue never
    # does - the client then keeps whatever `params_devices` supplied.
    params: int | None
    stan_json: str | None  # raw seed for the initial value, applied by the client


@dataclass(slots=True)
class SnapshotEntry:
    """One object's entry in a bulk `data/states` snapshot."""

    id: int
    stan_json: str | None


@dataclass(slots=True)
class StateUpdate:
    """A live state push for a single object."""

    id: int
    value: str
    on_ms: int | float | None
    tilt: int | None  # `lammel` percent, present only for tilt-capable covers


@dataclass(slots=True)
class ModuleDiagnostics:
    """A module's self-reported health from its `b/4F` broadcast."""

    supply_voltage: float  # volts on the CAN bus
    temperature: float | None  # °C, None on modules without the sensor


@dataclass(slots=True)
class StanJsonSeed:
    """Initial `state` value and server timestamp extracted from `stan_json`."""

    value: str | None
    on_ms: int | float | None
    tilt: int | None


def server_below_baseline(version: str | None) -> bool:
    """Whether a self-reported ``serverVersion`` is below the tested baseline.

    Missing or unparseable versions count as below - every baseline server
    reports one. Handles the observed plain build-number form (``"1865"``)
    and dotted forms, compared numerically part by part.
    """
    if not version:
        return True
    try:
        parts = tuple(int(p) for p in version.split("."))
    except ValueError:
        return True
    return parts < BASELINE_SERVER_VERSION


def warn_if_below_baseline(version: str | None) -> None:
    """Log the one below-baseline warning both discovery paths share."""
    if server_below_baseline(version):
        logging.getLogger(__name__).warning(
            "Ampio server reports version %s, below the tested baseline %s; "
            "behavior on this server is untested - upgrade the M-SERV",
            version or "(none)",
            ".".join(map(str, BASELINE_SERVER_VERSION)),
        )


def list_rows(payload: str) -> list[Any] | None:
    """Rows of a ``{"List": [...]}`` reply, or None if the payload is not one.

    The M-SERV is the only expected publisher on these topics, but nothing on
    the broker enforces that, and a reply of the wrong shape must not reach the
    row loops - they index and attribute-access every row.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    rows = data.get("List")
    return rows if isinstance(rows, list) else None


def to_int(value: Any) -> int | None:
    """Best-effort int coercion that returns None on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_details(payload: str) -> list[ObjectMetadata] | None:
    """Parse a `devicesDetails` payload into per-object metadata.

    Also parses the app-sync `data/devices` catalogue, which shares the row
    shape minus the `params` and `stan_json` columns (those read as 0 / None).

    Returns None when the payload is not parseable JSON; an empty list is a
    valid (empty) response.
    """
    rows = list_rows(payload)
    if rows is None:
        return None
    out: list[ObjectMetadata] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        oid = to_int(item.get("id"))
        if oid is None:
            continue
        out.append(
            ObjectMetadata(
                id=oid,
                device_id=to_int(item.get("id_urzadzenia")),
                typ_komponentu=item.get("typ_komponentu"),
                name=item.get("opis_menu") or None,
                interpretacja=to_int(item.get("interpretacja")),
                funkcja=to_int(item.get("funkcja")),
                leaf_id=_parse_leaf_id(item.get("leafId")),
                # `params` can exceed 32 bits (the matter-exposed flag is bit
                # 37), which Python ints handle natively.
                params=to_int(item.get("params")),
                stan_json=item.get("stan_json") or None,
            )
        )
    return out


def _parse_leaf_id(value: Any) -> str:
    """Coerce the `leafId` field to a string.

    The M-SERV emits an empty string for ghost rows (object removed from the
    Designer tree, DB row still returned) and for system objects (presence
    simulation / detection types). For everything else the value is a short
    underscored token like ``0_cb8f_76_0_0`` whose meaning is opaque - the
    library uses it only as the binary visibility marker.
    """
    return value if isinstance(value, str) else ""


def parse_devices(payload: str) -> list[AmpioModule] | None:
    """Parse a `devices` payload into a list of physical modules.

    Returned modules have `last_seen=None`; the caller preserves any existing
    `last_seen` from a prior discovery.
    """
    rows = list_rows(payload)
    if rows is None:
        return None
    out: list[AmpioModule] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        mid = to_int(item.get("id"))
        if mid is None:
            continue
        typ = to_int(item.get("typ_urzadzenia"))
        out.append(
            AmpioModule(
                id=mid,
                mac=to_int(item.get("mac")),
                mac_global=to_int(item.get("mac_global")),
                name=item.get("nazwa_urzadzenia") or None,
                type=typ,
                model=module_model(typ),
                sw_version=to_int(item.get("wersja_softu")),
                hw_version=to_int(item.get("wersja_pcb")),
            )
        )
    return out


def parse_params_devices(payload: str) -> dict[int, int] | None:
    """Parse a `data/params_devices` payload into `{object_id: params}`.

    The table covers the full object catalogue regardless of the account's
    grants. Returns None when the payload is not parseable JSON.
    """
    rows = list_rows(payload)
    if rows is None:
        return None
    out: dict[int, int] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        oid = to_int(item.get("id"))
        if oid is None:
            continue
        out[oid] = to_int(item.get("params")) or 0
    return out


def parse_scenes(payload: str) -> list[AmpioScene] | None:
    """Parse a `data/scenes` payload into the scene catalogue.

    Each row carries its actions twice - `Actions` as the wire command strings
    and `Infos` as their structured form. Only the object ids are kept, since
    the M-SERV replays the actions itself when a scene is run.
    """
    rows = list_rows(payload)
    if rows is None:
        return None
    out: list[AmpioScene] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        sid = to_int(item.get("id"))
        if sid is None:
            continue
        parent = to_int(item.get("parentId"))
        # A row without the column reads enabled, matching the dataclass
        # default: the app creates scenes enabled, and surfacing a scene of
        # unknown state beats silently hiding the catalogue if the column
        # ever drifts.
        raw_active = to_int(item.get("active"))
        # Malformed row fields degrade in the same spirit as `active` above:
        # the scene itself is real and runnable (the M-SERV replays its
        # actions server-side), so a broken annex must not hide it - and the
        # store's parse gate covers only the outer List shape, so nothing
        # row-shaped may escape fetch_scenes as a bare exception.
        infos = item.get("Infos")
        objects = {
            oid
            for info in (infos if isinstance(infos, list) else [])
            if isinstance(info, dict) and (oid := to_int(info.get("id"))) is not None
        }
        name = item.get("sceneName")
        out.append(
            AmpioScene(
                id=sid,
                name=name if isinstance(name, str) else "",
                active=raw_active != 0 if raw_active is not None else True,
                parent_id=parent if parent is not None and parent >= 0 else None,
                object_ids=frozenset(objects),
            )
        )
    return out


def parse_rooms(groups_payload: str, group_devices_payload: str) -> dict[int, str]:
    """Join `data/groups` and `data/group_devices` replies into a room map.

    Returns ``{ampio_object_id: room_name}``. Objects assigned to multiple
    groups map to the first room encountered - the join table has no
    "primary group" marker, and the intended consumer (a Home Assistant
    integration forwarding the value as ``DeviceInfo.suggested_area``)
    allows one area per device. Mistyped rows are skipped.
    """
    group_names: dict[int, str] = {}
    for row in list_rows(groups_payload) or []:
        if not isinstance(row, dict):
            continue
        gid = row.get("id")
        name = row.get("opis_menu")
        if isinstance(gid, int) and isinstance(name, str) and name:
            group_names[gid] = name
    room_map: dict[int, str] = {}
    for row in list_rows(group_devices_payload) or []:
        if not isinstance(row, dict):
            continue
        oid = row.get("id_obiektu")
        gid = row.get("id_grupy")
        if not isinstance(oid, int) or not isinstance(gid, int):
            continue
        if oid in room_map:
            continue  # first match wins; HA allows one area per device
        name = group_names.get(gid)
        if name:
            room_map[oid] = name
    return room_map


def _to_str(value: Any) -> str | None:
    """Coerce a field to a non-empty string, or None.

    The info fields are typed as strings; coercing keeps that true even if
    a number arrives on the wire - `server_below_baseline` splits the
    version, so a non-str value there would raise instead of comparing.
    """
    return str(value) if value not in (None, "") else None


def parse_server_info(payload: str) -> AmpioServerInfo | None:
    """Parse a server-info payload, keeping only the safe fields.

    The baseline server wraps the fields in a ``Results`` object and always
    reports its ``mac`` - the identity every consumer scopes a registry by.
    A payload without either is unparseable, exactly as the sibling parsers
    report a corrupt reply, so a parsed info always carries a populated
    :pyattr:`AmpioServerInfo.key`.
    """
    try:
        outer = json.loads(payload)
    except (ValueError, TypeError):
        return None
    data = outer.get("Results") if isinstance(outer, dict) else None
    if not isinstance(data, dict):
        return None
    mac = to_int(data.get("mac"))
    if mac is None:
        return None
    return AmpioServerInfo(
        mac=mac,
        user_id=to_int(data.get("userId")),
        server_version=_to_str(data.get("serverVersion")),
        server_revision=_to_str(data.get("serverRevision")),
        mqtt_version=_to_str(data.get("mqttVersion")),
        local_ip=_to_str(data.get("local_ip")),
        device_id=_to_str(data.get("device_id")),
    )


def parse_states_snapshot(payload: str) -> list[SnapshotEntry] | None:
    """Parse a bulk `data/states` snapshot."""
    rows = list_rows(payload)
    if rows is None:
        return None
    out: list[SnapshotEntry] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        oid = to_int(item.get("id"))
        if oid is None:
            continue
        out.append(SnapshotEntry(id=oid, stan_json=item.get("stan_json") or None))
    return out


def _parse_state_payload(oid: int, payload: str) -> StateUpdate:
    """Parse a live per-object state payload into a `StateUpdate`.

    The payload may be plain text or a JSON object with a `state` field; in
    either case `value` is set, and `on_ms` is populated when the payload
    carried a server timestamp.
    """
    value: str = payload
    on_ms: int | float | None = None
    tilt: int | None = None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        # Numeric `state` values arrive as int/float from JSON; the library
        # contract is text, so coerce here rather than at every consumer.
        raw_state = data.get("state")
        if raw_state is not None:
            value = str(raw_state)
        raw_on = data.get("on")
        if isinstance(raw_on, (int, float)):
            on_ms = raw_on
        tilt = to_int(data.get("lammel"))
    return StateUpdate(id=oid, value=value, on_ms=on_ms, tilt=tilt)


def parse_diagnostics(payload: str) -> ModuleDiagnostics | None:
    """Parse a `b/4F` diagnostics frame into supply voltage and temperature.

    The frame is `{"d": [0xFE, 0x4F, voltage, temperature], "m": mac}`.
    Voltage is in 0.2 V steps; temperature is offset by 100 °C and reads 0 on
    the modules that carry no temperature sensor. Returns None when the payload
    is not a diagnostics frame.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    frame = data.get("d")
    if not isinstance(frame, list) or len(frame) < 3:
        return None
    if frame[0] != 0xFE or frame[1] != 0x4F:
        return None
    voltage_byte = to_int(frame[2])
    if voltage_byte is None:
        return None
    voltage = voltage_byte * 0.2
    raw_temp = to_int(frame[3]) or 0 if len(frame) > 3 else 0
    return ModuleDiagnostics(
        supply_voltage=round(voltage, 1),
        temperature=float(raw_temp - 100) if raw_temp else None,
    )


def parse_stan_json(stan_json: str) -> StanJsonSeed | None:
    """Parse a `stan_json` blob into an initial value and server timestamp."""
    if not stan_json:
        return None
    try:
        data = json.loads(stan_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw_on = data.get("on")
    on_ms = raw_on if isinstance(raw_on, (int, float)) else None
    raw_state = data.get("state")
    return StanJsonSeed(
        value=str(raw_state) if raw_state is not None else None,
        on_ms=on_ms,
        tilt=to_int(data.get("lammel")),
    )


# --- Endpoint table --------------------------------------------------------
#
# Every request/response endpoint the M-SERV exposes is one row here, and that
# row is the single source of truth: the client derives its subscriptions,
# topic-to-handler routing, discovery-completion signals, and retained payloads
# from this table. Adding an endpoint is one row, not edits in four places:
# verify the wire shape live first (tools/probe_config.py publishes candidate
# keywords and prints the replies), add the row, give the reply an
# `AmpioStore._handlers` entry only if it mutates state, and expose a
# `fetch_<name>()` awaiting `AmpioClient._fetch` - `fetch_scenes()` is the
# three-line reference shape.
#
# A request publishes ``req_payload`` (a keyword, or "" for the dedicated
# ``states``/``info`` surfaces) to ``ampio/control/<user>/<req_surface>``; the
# reply lands on ``ampio/fromDB/<user>/<resp_surface>/<resp_leaf>``.


# The reserved administrator login. The app refuses to create a user of
# this name, and the broker authenticates it at CONNACK, so holding a
# session under it IS being the administrator - the account tier is a
# constructor fact, not a discovered one.
ADMIN_USERNAME = "admin"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One M-SERV request/response endpoint."""

    name: str
    req_surface: str  # control sub-topic: "config" | "states" | "info" | "data"
    req_payload: str  # request keyword, or "" for the states/info surfaces
    resp_surface: str  # fromDB sub-topic: "config" | "data"
    resp_leaf: str  # final response-topic segment
    # Part of the initial-discovery set awaited by start() /
    # wait_for_initial_discovery(). The rooms/scenes endpoints are on-demand.
    initial: bool = False
    # The one tier this endpoint answers for, or None for both. The M-SERV
    # serves the `config` catalogues to administrators only, and an admin
    # session never needs the app-sync pair (it repeats the `config` view).
    tier: AccessTier | None = None
    # The reply parser gating a pure request/response endpoint: a reply
    # that does not parse must neither resolve a fetch nor latch discovery,
    # so the gate IS the parser a fetch will run - never a stand-in shape
    # check. Endpoints whose replies mutate state are gated by their
    # AmpioStore handler instead and never read this field.
    parses: Callable[[str], object | None] = list_rows


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(
        "details",
        "config",
        "devicesDetails",
        "config",
        "devicesDetails",
        initial=True,
        tier=AccessTier.ADMIN,
    ),
    Endpoint(
        "devices",
        "config",
        "devices",
        "config",
        "devices",
        initial=True,
        tier=AccessTier.ADMIN,
    ),
    Endpoint("states", "states", "", "data", "states", initial=True),
    Endpoint("info", "info", "", "data", "info", initial=True),
    # App-sync object catalogue. Same wire keyword as the module list above but
    # on the `data` surface, and a different payload: DB objects (the
    # `devicesDetails` row shape minus `params`/`stan_json`), filtered to the
    # objects the account was granted in the Ampio app.
    Endpoint(
        "data_devices",
        "data",
        "devices",
        "data",
        "devices",
        initial=True,
        tier=AccessTier.RESTRICTED,
    ),
    # Per-object `params` bitfields for the app-sync catalogue. NOT
    # grant-filtered: every account receives the full table, which is what
    # lets a restricted account apply the hidden-flag visibility rule.
    Endpoint(
        "params_devices",
        "data",
        "params_devices",
        "data",
        "params_devices",
        initial=True,
        tier=AccessTier.RESTRICTED,
    ),
    Endpoint("groups", "data", "groups", "data", "groups"),
    Endpoint("group_devices", "data", "group_devices", "data", "group_devices"),
    Endpoint("scenes", "data", "scenes", "data", "scenes", parses=parse_scenes),
)

ENDPOINT_BY_NAME: dict[str, Endpoint] = {ep.name: ep for ep in ENDPOINTS}


# The M-SERV software baseline this library is developed and live-tested
# against, as the server self-reports it on the info surface. This is the
# compatibility floor, not a promise about anything older: a lower (or
# missing) serverVersion logs a warning at discovery and behavior on such a
# server is undefined - the fix is upgrading the M-SERV. The baseline server
# also reported serverRevision 409 and mqttVersion 5.133.11, recorded in the
# README; only serverVersion is compared.
BASELINE_SERVER_VERSION = (1865,)


# --- Commands --------------------------------------------------------------
#
# Writes go to one control topic per account as plain text:
# ``/api/set/<object_id>/<verb>[/<arg>...]``. The verb vocabulary is the
# M-SERV's own HTTP API, re-exposed over MQTT; see docs/protocol.md for
# the verb table.
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


# --- topic routing ---------------------------------------------------------


@dataclass(slots=True, frozen=True)
class EndpointReply:
    """A reply on a request/response endpoint, payload unparsed.

    Which parser applies is per-endpoint business - the store's handler
    table decides; the router only identifies the endpoint.
    """

    endpoint: Endpoint
    payload: str


@dataclass(slots=True, frozen=True)
class RawChannelEdge:
    """A decoded CAN channel edge from the raw `ampio/from/<MAC>` tree."""

    # Effective bus address (the hex topic segment parsed as an int, so
    # leading-zero / case differences never matter); matches `AmpioModule.mac`.
    mac: int
    prefix: str  # channel-type prefix ("f" flags, "i" digital inputs, ...)
    channel: int
    value: str


@dataclass(slots=True, frozen=True)
class DiagnosticsReport:
    """A module's parsed `b/4F` health broadcast with its sender mac."""

    mac: int
    diagnostics: ModuleDiagnostics


# Everything one MQTT message can classify into. `BusEvent` is the public
# event class itself - for bus events the wire message IS the event.
Inbound = EndpointReply | StateUpdate | RawChannelEdge | DiagnosticsReport | BusEvent


class Router:
    """Classifies one MQTT message into a typed inbound message, or None.

    The single home of topic-shape knowledge: every guard lives here, once,
    and anything unroutable - an unknown shape, a non-hex mac, a non-integer
    object id, channel, or event number, an unparseable diagnostics frame -
    returns None. The store then applies typed messages and never inspects a
    topic. ``endpoints`` is the subset the connection subscribes to (the
    account tier's served surfaces), so a reply topic outside it is
    unroutable like any other unknown shape. Endpoint reply and per-object
    state topics are namespaced by the connecting account (hence ``user``);
    the raw ``ampio/from`` tree is global.
    """

    __slots__ = ("_by_response", "_user")

    def __init__(self, user: str, endpoints: tuple[Endpoint, ...]) -> None:
        self._user = user
        self._by_response: dict[str, Endpoint] = {
            response_topic(ep, user): ep for ep in endpoints
        }

    def route(self, topic: str, payload: str) -> Inbound | None:
        endpoint = self._by_response.get(topic)
        if endpoint is not None:
            return EndpointReply(endpoint=endpoint, payload=payload)
        parts = topic.split("/")
        # The subscription is already scoped to the account's namespace, but
        # the router owns the topic shape and must not rely on who subscribed.
        if (
            len(parts) == 6
            and parts[0] == "ampio"
            and parts[1] == "fromDB"
            and parts[2] == self._user
            and parts[3] == "ob"
            and parts[5] == "state"
        ):
            oid = to_int(parts[4])
            return None if oid is None else _parse_state_payload(oid, payload)
        if len(parts) < 4 or parts[0] != "ampio" or parts[1] != "from":
            return None
        try:
            mac = int(parts[2], 16)
        except ValueError:
            return None
        if len(parts) == 6 and parts[3] == "state":
            channel = to_int(parts[5])
            if channel is None:
                return None
            return RawChannelEdge(
                mac=mac, prefix=parts[4], channel=channel, value=payload.strip()
            )
        if len(parts) == 5 and parts[3] == "b" and parts[4].upper() == "4F":
            diagnostics = parse_diagnostics(payload)
            if diagnostics is None:
                return None
            return DiagnosticsReport(mac=mac, diagnostics=diagnostics)
        if len(parts) == 4 and parts[3] == "event":
            number = to_int(payload.strip())
            return None if number is None else BusEvent(number=number, mac=mac)
        return None
