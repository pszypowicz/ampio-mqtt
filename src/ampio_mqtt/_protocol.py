"""Pure parsers for the Ampio DB-object MQTT protocol.

These helpers turn raw MQTT payloads into typed structures with no I/O and no
state mutation - the `AmpioStore` is what applies them to `AmpioState`.
Keeping the parsing isolated makes it trivially unit-testable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .device_types import module_capabilities, module_model
from .endpoints import BASELINE_SERVER_VERSION, ENDPOINTS, Endpoint, response_topic
from .events import BusEvent
from .models import AmpioModule, AmpioScene, AmpioServerInfo


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


def _rows(payload: str) -> list[Any] | None:
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
    rows = _rows(payload)
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
    rows = _rows(payload)
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
                capabilities=module_capabilities(typ) or frozenset(),
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
    rows = _rows(payload)
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
    rows = _rows(payload)
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
        objects = {
            oid
            for info in item.get("Infos", [])
            if isinstance(info, dict) and (oid := to_int(info.get("id"))) is not None
        }
        out.append(
            AmpioScene(
                id=sid,
                name=item.get("sceneName") or "",
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
    for row in _rows(groups_payload) or []:
        if not isinstance(row, dict):
            continue
        gid = row.get("id")
        name = row.get("opis_menu")
        if isinstance(gid, int) and isinstance(name, str) and name:
            group_names[gid] = name
    room_map: dict[int, str] = {}
    for row in _rows(group_devices_payload) or []:
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


def parse_locations(payload: str) -> dict[int, str]:
    """``{location_id: name}`` from a `config/locations` reply.

    The name table behind the Designer's "Lokalizacja" dropdown; rows with
    a missing id or an empty name are skipped.
    """
    out: dict[int, str] = {}
    for row in _rows(payload) or []:
        if not isinstance(row, dict):
            continue
        lid = row.get("id")
        name = row.get("opis_menu")
        if isinstance(lid, int) and isinstance(name, str) and name:
            out[lid] = name
    return out


def parse_server_info(payload: str) -> AmpioServerInfo:
    """Parse a server-info payload, keeping only the safe fields.

    The baseline server wraps the fields in a ``Results`` object; anything
    else parses as empty. Returns an empty `AmpioServerInfo` on parse
    failure - callers treat that as "info not available yet" rather than an
    error.
    """
    try:
        outer = json.loads(payload)
    except (ValueError, TypeError):
        return AmpioServerInfo()
    data = outer.get("Results") if isinstance(outer, dict) else None
    if not isinstance(data, dict):
        return AmpioServerInfo()
    return AmpioServerInfo(
        mac=to_int(data.get("mac")),
        user_id=to_int(data.get("userId")),
        server_version=data.get("serverVersion") or None,
        server_revision=data.get("serverRevision") or None,
        mqtt_version=data.get("mqttVersion") or None,
        local_ip=data.get("local_ip") or None,
        device_id=data.get("device_id") or None,
    )


def parse_states_snapshot(payload: str) -> list[SnapshotEntry] | None:
    """Parse a bulk `data/states` snapshot."""
    rows = _rows(payload)
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
        tilt = _parse_lammel(data)
    return StateUpdate(id=oid, value=value, on_ms=on_ms, tilt=tilt)


def _parse_lammel(data: dict[str, Any]) -> int | None:
    """Read the `lammel` slat angle percent from a state payload."""
    return to_int(data.get("lammel"))


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
        tilt=_parse_lammel(data),
    )


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
    topic. Endpoint reply and per-object state topics are namespaced by the
    connecting account (hence ``user``); the raw ``ampio/from`` tree is
    global.
    """

    __slots__ = ("_by_response",)

    def __init__(self, user: str) -> None:
        self._by_response: dict[str, Endpoint] = {
            response_topic(ep, user): ep for ep in ENDPOINTS
        }

    def route(self, topic: str, payload: str) -> Inbound | None:
        endpoint = self._by_response.get(topic)
        if endpoint is not None:
            return EndpointReply(endpoint=endpoint, payload=payload)
        parts = topic.split("/")
        if (
            len(parts) == 6
            and parts[0] == "ampio"
            and parts[1] == "fromDB"
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
