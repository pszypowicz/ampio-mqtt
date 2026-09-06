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

import base64
import binascii
import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .events import BusEventRaised
from .models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    DesignerRecord,
    ModuleRecord,
    ThermostatState,
)


@dataclass(slots=True)
class ObjectMetadata:
    """Per-object metadata from a `devicesDetails` payload."""

    id: int
    id_urzadzenia: int | None
    typ_komponentu: str | None
    opis_menu: str | None
    interpretacja: int | None
    funkcja: int | None  # physical channel index within the module
    leaf_id: str  # `leafId`; empty for system objects, and after a Matter uncheck
    # `params` bitfield; bit 4 = hidden/stub, bit 37 = matter-exposed. None
    # when the reply carried no such column, which the app-sync catalogue never
    # does - the client then keeps whatever `params_devices` supplied.
    params: int | None
    # `type` column: the Matter device type ID assigned in Designer, carried
    # as a decimal string on the wire ("256" = 0x0100 On/Off Light). Empty or
    # null when the object has no tag - both read as None. docs/identity.md
    # holds the vocabulary.
    matter_device_type: int | None
    # `czas` column converted to milliseconds (the wire unit is 10 ms ticks):
    # Designer's per-object time, the app's default pulse length. None when
    # the reply carried no such column, which the app-sync catalogue never
    # does - the client then keeps whatever `params_devices` supplied.
    pulse_ms: int | None
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
    state: str
    on_ms: int | float | None
    lammel: int | None  # Percent, present only for tilt-capable covers
    # Climate readback, present only in the rich `reg` push shape.
    thermostat: ThermostatState | None = None


@dataclass(slots=True)
class ModuleDiagnostics:
    """A module's self-reported health from its `b/4F` broadcast."""

    supply_voltage: float  # volts on the CAN bus
    temperature: float | None  # °C, None on modules without the sensor


@dataclass(slots=True)
class StanJsonSeed:
    """Initial `state` value and server timestamp extracted from `stan_json`."""

    state: str | None
    on_ms: int | float | None
    lammel: int | None
    # Climate readback, present only in the rich `reg` snapshot shape.
    thermostat: ThermostatState | None = None


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
                id_urzadzenia=to_int(item.get("id_urzadzenia")),
                typ_komponentu=item.get("typ_komponentu"),
                opis_menu=item.get("opis_menu") or None,
                interpretacja=to_int(item.get("interpretacja")),
                funkcja=to_int(item.get("funkcja")),
                leaf_id=_parse_leaf_id(item.get("leafId")),
                # `params` can exceed 32 bits (the matter-exposed flag is bit
                # 37), which Python ints handle natively.
                params=to_int(item.get("params")),
                matter_device_type=to_int(item.get("type")),
                pulse_ms=_czas_to_pulse_ms(item.get("czas")),
                stan_json=item.get("stan_json") or None,
            )
        )
    return out


def _czas_to_pulse_ms(value: Any) -> int | None:
    """The `czas` column in milliseconds, or None when absent / not a number."""
    czas = to_int(value)
    return czas * 10 if czas is not None else None


def _parse_leaf_id(value: Any) -> str:
    """Coerce the `leafId` field to a string.

    The M-SERV emits an empty string or null for system objects (presence
    simulation / detection types) and for an object whose Matter box was
    unchecked in Designer. For everything else the value is a short
    underscored token like ``0_cb8f_76_0_0``, which the Designer reads as
    ``macGroup``, ``mac``, ``sfId``, ``subSfId``, and ``ioNo``. This parse
    keeps the raw string.
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
        out.append(
            AmpioModule(
                id=mid,
                mac=to_int(item.get("mac")),
                mac_global=to_int(item.get("mac_global")),
                nazwa_urzadzenia=item.get("nazwa_urzadzenia") or None,
                typ_urzadzenia=to_int(item.get("typ_urzadzenia")),
                wersja_softu=to_int(item.get("wersja_softu")),
                wersja_pcb=to_int(item.get("wersja_pcb")),
            )
        )
    return out


@dataclass(slots=True, frozen=True)
class ParamsEntry:
    """One object's row in the ``data/params_devices`` table."""

    params: int
    pulse_ms: int


def parse_params_devices(payload: str) -> dict[int, ParamsEntry] | None:
    """Parse a `data/params_devices` payload into per-object config facts.

    The table covers the full object catalogue regardless of the account's
    grants, and it is complete: an absent column reads as the off value, not
    as unknown. Returns None when the payload is not parseable JSON.
    """
    rows = list_rows(payload)
    if rows is None:
        return None
    out: dict[int, ParamsEntry] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        oid = to_int(item.get("id"))
        if oid is None:
            continue
        out[oid] = ParamsEntry(
            params=to_int(item.get("params")) or 0,
            pulse_ms=_czas_to_pulse_ms(item.get("czas")) or 0,
        )
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
        # Malformed row fields degrade instead of hiding the scene: it is
        # real and runnable (the M-SERV replays its actions server-side),
        # and the parse gate covers only the outer List shape, so nothing
        # row-shaped may escape fetch_scenes as a bare exception. A row
        # without `active` reads enabled, the state the app creates.
        raw_active = to_int(item.get("active"))
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
                scene_name=name if isinstance(name, str) else "",
                active=raw_active != 0 if raw_active is not None else True,
                parent_id=parent if parent is not None and parent >= 0 else None,
                object_ids=frozenset(objects),
            )
        )
    return out


def parse_rooms(
    groups_rows: list[Any], group_devices_rows: list[Any]
) -> dict[int, str]:
    """Join parsed `data/groups` and `data/group_devices` rows into a room map.

    Returns ``{ampio_object_id: room_name}``. Objects assigned to multiple
    groups map to the first room encountered - the join table has no
    "primary group" marker, and the intended consumer (a Home Assistant
    integration forwarding the value as ``DeviceInfo.suggested_area``)
    allows one area per device. Mistyped rows are skipped.
    """
    group_names: dict[int, str] = {}
    for row in groups_rows:
        if not isinstance(row, dict):
            continue
        gid = row.get("id")
        name = row.get("opis_menu")
        if isinstance(gid, int) and isinstance(name, str) and name:
            group_names[gid] = name
    room_map: dict[int, str] = {}
    for row in group_devices_rows:
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


def parse_locations(payload: str) -> dict[int, str] | None:
    """``{location_id: name}`` from a `config/locations` reply.

    The name table behind the Designer's "Lokalizacja" dropdown; rows with
    a missing id or an empty name are skipped. None when the payload is
    not a ``{"List": [...]}`` document.
    """
    rows = list_rows(payload)
    if rows is None:
        return None
    out: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        lid = to_int(row.get("id"))
        name = row.get("opis_menu")
        if lid is not None and isinstance(name, str) and name:
            out[lid] = name
    return out


@dataclass(slots=True, frozen=True)
class OutputDescription:
    """One per-output entry of a module's CAN-resident description record."""

    desc_type: int  # description class (OUTPUTS=12, ROLLER=26, ...)
    out_no: int  # output index within the class
    out_loc: int  # pointer into the locations name table; 0 = unassigned
    out_type: int  # Matter device type; 0 = untagged
    desc: str


def parse_descriptions_blob(blob: bytes) -> tuple[OutputDescription, ...]:
    """Decode the flat description frames.

    ``[len:2][descType:2][outNo:2][outLoc:2][outType:2][utf8 desc]``,
    little-endian, repeated; ``len`` counts the whole frame. A length
    below the 10-byte header or past the end stops the walk - the
    remainder is unreadable either way.
    """
    out: list[OutputDescription] = []
    offset = 0
    while offset + 10 <= len(blob):
        length = int.from_bytes(blob[offset : offset + 2], "little")
        if length < 10 or offset + length > len(blob):
            break
        out.append(
            OutputDescription(
                desc_type=int.from_bytes(blob[offset + 2 : offset + 4], "little"),
                out_no=int.from_bytes(blob[offset + 4 : offset + 6], "little"),
                out_loc=int.from_bytes(blob[offset + 6 : offset + 8], "little"),
                out_type=int.from_bytes(blob[offset + 8 : offset + 10], "little"),
                desc=blob[offset + 10 : offset + length].decode("utf-8", "replace"),
            )
        )
        offset += length
    return tuple(out)


@dataclass(slots=True, frozen=True)
class DeviceRecord:
    """One device of a ``device_api/from/list`` reply with its record.

    ``mac`` is the override (``macUser``): the id every leaf embeds and
    ``AmpioModule.mac`` carries. ``mac_global`` is the factory id
    (``macProd``): the id the device_api tree itself is keyed by.
    """

    mac: int
    mac_global: int
    entries: tuple[OutputDescription, ...]


def _decode_descriptions(raw: object) -> tuple[OutputDescription, ...] | None:
    """The decoded ``descriptions`` field: empty when absent, None when unreadable."""
    if raw in (None, ""):
        return ()
    if not isinstance(raw, str):
        return None
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    return parse_descriptions_blob(blob)


def parse_device_list(payload: str) -> tuple[DeviceRecord, ...] | None:
    """Every device of a ``device_api/from/list`` reply, with its record.

    None when the payload is not a JSON object with a ``devices`` list. A
    device without a ``descriptions`` field reads empty - no descriptions
    written. A device whose ids do not parse or whose blob is unreadable
    is left out, so it counts as unlisted.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        return None
    out: list[DeviceRecord] = []
    for item in data["devices"]:
        if not isinstance(item, dict):
            continue
        mac = to_int(item.get("macUser"))
        mac_global = to_int(item.get("macProd"))
        entries = _decode_descriptions(item.get("descriptions"))
        if mac is None or mac_global is None or entries is None:
            continue
        out.append(DeviceRecord(mac=mac, mac_global=mac_global, entries=entries))
    return tuple(out)


# Designer's cleared-entry form, live-proven: a clear never deletes the
# frame, it rewrites it in place with `out_loc` 0x3FFF, `out_type` 0 and
# the placeholder description "." - so both sentinels read as absent.
_UNASSIGNED_OUT_LOC = 0x3FFF
_EMPTY_DESC = "."


def _entry_location(
    entry: OutputDescription, location_names: Mapping[int, str]
) -> str | None:
    if entry.out_loc in (0, _UNASSIGNED_OUT_LOC):
        return None
    return location_names.get(entry.out_loc)


def _entry_desc(entry: OutputDescription) -> str | None:
    return None if entry.desc in ("", _EMPTY_DESC) else entry.desc


def resolve_designer(
    objects: Mapping[int, AmpioObject],
    descriptions_by_mac: Mapping[int, tuple[OutputDescription, ...]],
    location_names: Mapping[int, str],
    colliding_macs: frozenset[int],
) -> dict[int, DesignerRecord]:
    """Join each object to its module's description entry.

    The key is ``(DESC_TYPE_BY_KIND[typ_komponentu], leaf_io_no)`` within
    the module record of ``module_mac``. Objects on a colliding mac are
    skipped - the reply cannot be attributed to one module. ``out_loc`` 0
    or 16383 reads unassigned and ``out_type`` 0 untagged, so none
    produces a value. A ``desc`` that is empty or the ``.`` placeholder
    reads as None, like the other two fields.
    """
    entries_by_key = {
        mac: {(e.desc_type, e.out_no): e for e in entries}
        for mac, entries in descriptions_by_mac.items()
    }
    out: dict[int, DesignerRecord] = {}
    for obj in objects.values():
        desc_type = DESC_TYPE_BY_KIND.get(obj.typ_komponentu or "")
        mac = obj.module_mac
        out_no = obj.leaf_io_no
        if desc_type is None or mac is None or out_no is None:
            continue
        if mac in colliding_macs:
            continue
        entry = entries_by_key.get(mac, {}).get((desc_type, out_no))
        if entry is None:
            continue
        out[obj.id] = DesignerRecord(
            location=_entry_location(entry, location_names),
            matter_device_type=entry.out_type or None,
            desc=_entry_desc(entry),
        )
    return out


# The description class describing the module itself rather than one output:
# its `desc` is the module name and its `out_loc` the module-level location.
DEVICE_NAME_DESC_TYPE = 1


def resolve_module_records(
    descriptions_by_mac: Mapping[int, tuple[OutputDescription, ...]],
    location_names: Mapping[int, str],
    colliding_macs: frozenset[int],
) -> dict[int, ModuleRecord]:
    """The DEVICE_NAME record entry of every answering module, by mac.

    A record without the entry reads an empty bundle - the module
    answered, so the emptiness is authoritative. The unassigned and
    placeholder sentinels read None, exactly as ``resolve_designer``
    reads them. Colliding macs are skipped: the reply cannot be
    attributed.
    """
    out: dict[int, ModuleRecord] = {}
    for mac, entries in descriptions_by_mac.items():
        if mac in colliding_macs:
            continue
        entry = next((e for e in entries if e.desc_type == DEVICE_NAME_DESC_TYPE), None)
        if entry is None:
            out[mac] = ModuleRecord()
        else:
            out[mac] = ModuleRecord(
                location=_entry_location(entry, location_names),
                desc=_entry_desc(entry),
            )
    return out


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
    :pyattr:`AmpioServerInfo.server_key`.
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


# The mask Home Assistant's diagnostics redaction writes, reused so a
# redacted snapshot reads uniformly in a bug report.
REDACTED = "**REDACTED**"

# The info-reply keys whose values survive into the diagnostics copy. The
# retained payload is one string a consumer's key-based redactor cannot
# reach into, so every other value - the known private fields and any a
# future firmware adds - is masked at the source (#137).
_INFO_SAFE_KEYS = frozenset(
    {"Status", "mac", "userId", "serverVersion", "serverRevision", "mqttVersion"}
)


def redact_info_payload(payload: str) -> str:
    """The server-info reply with every non-safelisted value masked.

    Keys stay visible, so a report still shows the reply's shape. A reply
    without the parseable envelope is withheld outright: a truncated JSON
    string can carry the private fields in clear text.
    """
    try:
        outer = json.loads(payload)
    except (ValueError, TypeError):
        return REDACTED
    if not isinstance(outer, dict):
        return REDACTED
    results = outer.get("Results")
    if not isinstance(results, dict):
        return REDACTED
    masked_results = {
        key: value if key in _INFO_SAFE_KEYS else REDACTED
        for key, value in results.items()
    }
    masked = {
        key: masked_results
        if key == "Results"
        else (value if key in _INFO_SAFE_KEYS else REDACTED)
        for key, value in outer.items()
    }
    return json.dumps(masked)


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


def _finite_float(raw: object) -> float | None:
    """A finite float from a wire value (string on the wire), else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_thermostat(data: dict[str, Any]) -> ThermostatState | None:
    """The climate readback from a state dict, None unless reg-shaped.

    The rich `reg` shape is recognized by its own keys - `measureTemp`,
    `setTemperature`, `mode`, `cooling` - so every other object's push
    (including the cover shape with `block`) reads None.
    """
    if not any(
        key in data for key in ("measureTemp", "setTemperature", "mode", "cooling")
    ):
        return None
    raw_mode = data.get("mode")
    raw_cooling = data.get("cooling")
    return ThermostatState(
        measure_temp=_finite_float(data.get("measureTemp")),
        set_temperature=_finite_float(data.get("setTemperature")),
        mode=str(raw_mode) if raw_mode is not None else None,
        cooling=None if raw_cooling is None else str(raw_cooling) not in ("", "0"),
    )


def _parse_state_payload(oid: int, payload: str) -> StateUpdate:
    """Parse a live per-object state payload into a `StateUpdate`.

    The payload may be plain text or a JSON object with a `state` field; in
    either case `state` is set, and `on_ms` is populated when the payload
    carried a server timestamp. Plain text is stripped, exactly as the raw
    channel form is.
    """
    state: str = payload.strip()
    on_ms: int | float | None = None
    lammel: int | None = None
    thermostat: ThermostatState | None = None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        # Numeric `state` values arrive as int/float from JSON; the library
        # contract is text, so coerce here rather than at every consumer.
        raw_state = data.get("state")
        if raw_state is not None:
            state = str(raw_state)
        raw_on = data.get("on")
        if isinstance(raw_on, (int, float)):
            on_ms = raw_on
        lammel = to_int(data.get("lammel"))
        thermostat = _parse_thermostat(data)
    return StateUpdate(
        id=oid, state=state, on_ms=on_ms, lammel=lammel, thermostat=thermostat
    )


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
    """Parse a `stan_json` blob into an initial state and server timestamp."""
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
        state=str(raw_state) if raw_state is not None else None,
        on_ms=on_ms,
        lammel=to_int(data.get("lammel")),
        thermostat=_parse_thermostat(data),
    )


# --- Endpoint table --------------------------------------------------------
#
# One row per M-SERV request/response endpoint, and the row is the single
# source of truth: subscriptions, routing, discovery-completion signals,
# and retained payloads all derive from it. To add an endpoint: verify
# the wire shape live (tools/probe_config.py), add the row, give the
# reply an `AmpioStore._handlers` entry only if it mutates state, and
# expose a `fetch_<name>()` awaiting `AmpioClient._fetch` -
# `fetch_scenes()` is the reference shape.
#
# A request publishes ``req_payload`` (a keyword, or "" for the dedicated
# ``states``/``info`` surfaces) to ``ampio/control/<user>/<req_surface>``;
# the reply lands on ``ampio/fromDB/<user>/<resp_surface>/<resp_leaf>``.


# The reserved administrator login. The app refuses to create a user of
# this name and the broker authenticates it at CONNACK, so the account
# tier is a constructor fact, not a discovered one.
ADMIN_USERNAME = "admin"


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One M-SERV request/response endpoint."""

    name: str
    req_surface: str  # control sub-topic: "config" | "states" | "info" | "data"
    req_payload: str  # request keyword, or "" for the states/info surfaces
    resp_surface: str  # fromDB sub-topic: "config" | "data"
    resp_leaf: str  # final response-topic segment
    # Part of the initial-discovery set awaited by connect() /
    # wait_for_initial_discovery(). The rooms/scenes endpoints are on-demand.
    initial: bool = False
    # The one tier this endpoint answers for, or None for both. The M-SERV
    # serves the `config` catalogues to administrators only, and an admin
    # session never needs the app-sync pair (it repeats the `config` view).
    tier: AccessTier | None = None
    # The reply parser for a pure request/response endpoint. The dispatcher
    # runs it exactly once: a reply that does not parse neither resolves a
    # fetch nor latches discovery, and the parsed value is what a fetch
    # returns. None marks an endpoint whose reply mutates state - its
    # AmpioStore handler is the gate instead.
    parses: Callable[[str], object | None] | None = None
    # Rewrites the reply before it is retained for diagnostics_snapshot().
    # Set on an endpoint whose reply carries private fields: the retained
    # copy is one string a consumer's key-based redactor cannot reach
    # into. The store and the fetch parsers always read the raw reply.
    redacts: Callable[[str], str] | None = None


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
    Endpoint(
        "info", "info", "", "data", "info", initial=True, redacts=redact_info_payload
    ),
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
    Endpoint("groups", "data", "groups", "data", "groups", parses=list_rows),
    Endpoint(
        "group_devices",
        "data",
        "group_devices",
        "data",
        "group_devices",
        parses=list_rows,
    ),
    Endpoint("scenes", "data", "scenes", "data", "scenes", parses=parse_scenes),
    # The Designer "Lokalizacja" name table. On-demand; the per-output
    # pointer that resolves through it rides the device_api record
    # (resolve_records()).
    Endpoint(
        "locations",
        "config",
        "locations",
        "config",
        "locations",
        tier=AccessTier.ADMIN,
        parses=parse_locations,
    ),
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


# --- Raw CAN writes --------------------------------------------------------
#
# The admin-only `ampio/to/<machex>/raw` topic broadcasts a raw CAN frame
# from the M-SERV. Frame `[0x30, 0xF9, value, channel]` sets a module's
# output: 0x30 is the generic output-write function (the Designer SPA maps
# every output leaf to it) and 0xF9 the set-u8 command. It is the ONLY
# write that reaches a classic panel's binary outputs (status LEDs) - the
# `/api` verbs and the per-channel `o/<ch>/cmd` form are silently dropped
# for those, while a relay module answers all three. docs/protocol.md
# ("Panel outputs") carries the live evidence.


def raw_write_topic(mac: int) -> str:
    """The raw CAN write topic for one module, mac in lowercase hex."""
    return f"ampio/to/{mac:x}/raw"


def raw_output_payload(value: int, channel: int) -> str:
    """The set-output frame as the wire's ASCII hex form.

    ``channel`` is the 0-based output index - :pyattr:`AmpioObject.leaf_io_no`,
    one below the 1-based raw state channel.
    """
    return f"30f9{value:02x}{channel:02x}"


def request_topic(ep: Endpoint, user: str) -> str:
    """Control topic an endpoint's request keyword is published to."""
    return f"ampio/control/{user}/{ep.req_surface}"


def response_topic(ep: Endpoint, user: str) -> str:
    """fromDB topic an endpoint's reply arrives on."""
    return f"ampio/fromDB/{user}/{ep.resp_surface}/{ep.resp_leaf}"


def ob_state_wildcard(user: str) -> str:
    """Wildcard for all object state topics for an account."""
    return f"ampio/fromDB/{user}/ob/+/state"


# The raw `ampio/from/<MAC>/...` tree: global (not user-namespaced), retained,
# admin-only. docs/raw-channel-bridge.md is the home for why only the two
# on-change input prefixes are subscribed and the high-rate ones are not.
RAW_INPUT_WILDCARDS = ("ampio/from/+/state/f/+", "ampio/from/+/state/i/+")

# Binary output channels, bridged for `przekaznik` objects. A touch
# panel's status LEDs have no other retained surface, and every module's
# binary outputs share the channel shape (docs/raw-channel-bridge.md).
RAW_OUTPUT_WILDCARD = "ampio/from/+/state/o/+"

# Per-module diagnostics broadcasts (CAN supply voltage, own temperature).
RAW_DIAGNOSTICS_WILDCARD = "ampio/from/+/b/4F"

# Bus events (1-65535); receiving rides the admin-only raw tree, raising goes
# to the command surface - the rights model is in docs/protocol.md.
RAW_EVENT_WILDCARD = "ampio/from/+/event"


# The admin-only device_api tree. One `list` request returns every
# module's CAN-resident record in a single reply, the M-SERV's own
# included, each device tagged with both of its ids. The per-module
# get_data pair is keyed by the factory id and answers nothing on an
# override mac - docs/identity.md.
DEVICE_API_LIST_REQUEST = "device_api/to/list"
DEVICE_API_LIST_PAYLOAD = b"0"
DEVICE_API_LIST_TOPIC = "device_api/from/list"


# typ_komponentu -> description class (descType), live-proven pairs only
# (docs/identity.md): an unlisted kind resolves no location. Extend only
# with a live-proven pair.
DESC_TYPE_BY_KIND: dict[str, int] = {
    "przekaznik": 12,  # OUTPUTS
    "roleta_procenty": 26,  # ROLLER
    "roleta_lamelki": 26,  # ROLLER
    "led": 16,  # OUT_OC_U8
    "rgbw": 34,  # RGBW output class; no symbolic name in the recovered enum
}


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
    state: str


@dataclass(slots=True, frozen=True)
class DiagnosticsReport:
    """A module's parsed `b/4F` health broadcast with its sender mac."""

    mac: int
    diagnostics: ModuleDiagnostics


@dataclass(slots=True, frozen=True)
class DeviceList:
    """Every device's parsed record from a device_api list reply."""

    devices: tuple[DeviceRecord, ...]


# Everything one MQTT message can classify into. `BusEventRaised` is the
# public event class itself - for bus events the wire message IS the event.
Inbound = (
    EndpointReply
    | StateUpdate
    | RawChannelEdge
    | DiagnosticsReport
    | DeviceList
    | BusEventRaised
)


class Router:
    """Classifies one MQTT message into a typed inbound message, or None.

    The single home of topic-shape knowledge: anything unroutable returns
    None, and the store applies typed messages without inspecting a
    topic. ``endpoints`` is the tier's served subset, so a reply topic
    outside it is unroutable like any other unknown shape. Endpoint reply
    and per-object state topics are namespaced by the connecting account
    (hence ``user``); the raw ``ampio/from`` tree is global.
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
        if topic == DEVICE_API_LIST_TOPIC:
            devices = parse_device_list(payload)
            return None if devices is None else DeviceList(devices=devices)
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
                mac=mac, prefix=parts[4], channel=channel, state=payload.strip()
            )
        if len(parts) == 5 and parts[3] == "b" and parts[4] == "4F":
            diagnostics = parse_diagnostics(payload)
            if diagnostics is None:
                return None
            return DiagnosticsReport(mac=mac, diagnostics=diagnostics)
        if len(parts) == 4 and parts[3] == "event":
            number = to_int(payload.strip())
            return (
                None if number is None else BusEventRaised(event_number=number, mac=mac)
            )
        return None
