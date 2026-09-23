"""Everything wire-shaped for the Ampio DB-object MQTT protocol.

One module owns both directions: the endpoint table with its topic and
command builders (what the client says), and the pure parsers with the
Router (what the wire says back). No network I/O and no store mutation - the
`AmpioStore` applies the typed results to its state.

Topics are namespaced by the connecting account:
  state:     ampio/fromDB/<user>/ob/<id>/state   -> {"state","desc","on"}
  modules:   publish ampio/control/<user>/config = "devices"
             -> ampio/fromDB/<user>/config/devices = {"List":[...]}
  catalogue: publish ampio/control/<user>/data = "devices" or "params_devices"
             -> ampio/fromDB/<user>/data/<keyword> = {"List":[...]}
  digests:   ampio/fromDB/<user>/md5/<table> (retained) = MD5 of an app-sync
             table's reply, rewritten by the M-SERV on a Designer save

Each endpoint publishes on `ampio/control/<user>/<surface>`, where the
surface is `config`, `data`, `states`, or `info`. On `config` and `data` the
payload keyword selects the reply. The `states` and `info` requests carry an
empty payload. The `config` surface (module list, locations) answers only for
administrator accounts. Every account reads the object catalogue from the
`data` surface - see :class:`AccessTier` and the endpoint table below.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from .errors import AmpioProtocolError
from .events import BusEventRaised
from .models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    CoverParameters,
    DesignerRecord,
    ModuleAddress,
    ModuleFunction,
    ModuleRecord,
    PanelSettings,
    ThermostatState,
)


@dataclass(slots=True)
class ObjectMetadata:
    """One object-catalogue row, in the columns `data/devices` serves."""

    id: int
    typ_komponentu: str
    interpretacja: int
    funkcja: int  # physical channel index within the module
    # leafId, raw; empty for a system row and after a Matter check-then-uncheck,
    # the door decides
    leaf_id: str
    # the opis_menu column; empty reads None: the object carries no name
    name: str | None
    # `type` column: the Matter device type ID assigned in Designer, carried
    # as a decimal string on the wire ("256" = 0x0100 On/Off Light). Empty or
    # null when the object has no tag - both read as None.
    # docs/description-records.md holds the vocabulary.
    matter_device_type: int | None
    # Designer's "String format" column (a printf conversion, optionally
    # followed by a unit), verbatim. Empty when unset. `AmpioObject.unit`
    # and `AmpioObject.decimals` read it.
    format: str


# The two rows the M-SERV creates itself. Neither is a module output and
# neither carries a leaf. The store drops both by their type as it reads the
# catalogue, before the door reads any leaf.
SYSTEM_ROW_TYPES = frozenset(("detekcja", "symulacja"))


@dataclass(slots=True)
class SnapshotEntry:
    """One object's entry in a bulk `data/states` snapshot."""

    id: int
    stan_json: str


@dataclass(slots=True)
class StateUpdate:
    """A live state push for a single object."""

    id: int
    state: str
    # The M-SERV stamp the value was reported at, in ms.
    on_ms: int | float
    lammel: int | None  # Percent, present only for tilt-capable covers
    # Roller lock bits, present only on cover pushes.
    block: int | None = None
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

    state: str
    on_ms: int | float
    lammel: int | None
    # Roller lock bits, present only on cover rows.
    block: int | None = None
    # Climate readback, present only in the rich `reg` snapshot shape.
    thermostat: ThermostatState | None = None


def server_below_baseline(version: str | None) -> bool:
    """Whether a self-reported ``serverVersion`` is below the tested baseline.

    Missing or unparseable versions count as below - every baseline server
    reports one. Handles the plain build-number form (``"1865"``)
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
    """Log the below-baseline warning.

    The info handler and `check_connection` share it.
    """
    if server_below_baseline(version):
        logging.getLogger(__name__).warning(
            "Ampio server reports version %s, below the tested baseline %s; "
            "behavior on this server is untested - upgrade the M-SERV",
            version or "(none)",
            ".".join(map(str, BASELINE_SERVER_VERSION)),
        )


def decode_envelope(payload: str, surface: str) -> dict[str, Any]:
    """The JSON object a table reply wraps its content in.

    Raises :class:`AmpioProtocolError` when the payload is not a JSON object.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as err:
        raise AmpioProtocolError(f"The Ampio {surface} reply is not JSON") from err
    if not isinstance(data, dict):
        raise AmpioProtocolError(f"The Ampio {surface} reply is not a JSON object")
    return cast("dict[str, Any]", data)


def require_rows(data: Mapping[str, Any], surface: str) -> list[dict[str, Any]]:
    """The rows of a ``{"List": [...]}`` reply.

    Raises :class:`AmpioProtocolError` when `List` is not an array or a row is
    not an object. The M-SERV is the expected publisher of these replies, but
    the broker does not enforce that.
    """
    rows = data.get("List")
    if not isinstance(rows, list):
        raise AmpioProtocolError(f"The Ampio {surface} reply carries no `List` array")
    if not all(isinstance(row, dict) for row in rows):
        raise AmpioProtocolError(f"An Ampio {surface} row is not a JSON object")
    return cast("list[dict[str, Any]]", rows)


def to_int(value: Any) -> int | None:
    """Int coercion, None on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _column(row: Mapping[str, Any], column: str, surface: str) -> Any:
    """One served column's raw value, naming it when the row drops it."""
    if column not in row:
        raise AmpioProtocolError(f"An Ampio {surface} row carries no `{column}` column")
    return row[column]


def _int_column(row: Mapping[str, Any], column: str, surface: str) -> int:
    """One served column as an integer. The wire spells some of them as text."""
    value = to_int(_column(row, column, surface))
    if value is None:
        raise AmpioProtocolError(
            f"The `{column}` column of an Ampio {surface} row is not an integer"
        )
    return value


def _text_column(row: Mapping[str, Any], column: str, surface: str) -> str:
    """One served column as text. An empty string is a value."""
    value = _column(row, column, surface)
    if not isinstance(value, str):
        raise AmpioProtocolError(
            f"The `{column}` column of an Ampio {surface} row is not text"
        )
    return value


def _nullable_text_column(row: Mapping[str, Any], column: str, surface: str) -> str:
    """One served text column whose null reads as empty.

    Serves ``opis_menu``, ``format``, and ``nazwa_urzadzenia``. The M-SERV
    writes null in place of an empty string on some rows, and any non-string
    value reads as empty. The column must still be there: an absent one is a
    different reply shape.
    """
    value = _column(row, column, surface)
    return value if isinstance(value, str) else ""


def _leaf_column(row: Mapping[str, Any]) -> str:
    """The `leafId` column: the string as is, "" for a null value.

    Anything else raises :class:`AmpioProtocolError`.
    """
    value = _column(row, "leafId", _CATALOGUE)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    raise AmpioProtocolError(
        f"The Ampio object catalogue row {to_int(row.get('id'))} carries a "
        "leafId that is neither a string nor null"
    )


# The surface names the protocol errors speak, one per reply shape.
_CATALOGUE = "object catalogue"
_MODULE_LIST = "module list"
_PARAMS_TABLE = "params_devices table"
_SNAPSHOT = "states snapshot"
_SCENES = "scene catalogue"
_LOCATIONS = "locations table"
_INFO = "server info"
_PUSH = "state push"
_GROUPS = "room table"
_MEMBERSHIP = "room membership table"

# The `leafId` shape: `0_<macHex>_<sfId>_<subSfId>_<ioNo>`, a leading
# literal `0`, then the four fields the parse reads (docs/identity.md).
# Strict: a half-parsed address that is wrong is worse than a refused reply.
_LEAF_ID_RE = re.compile(r"0_([0-9a-fA-F]+)_(\d+)_(\d+)_(\d+)")


class LeafFault(AmpioProtocolError):
    """A ``leafId`` the library cannot read."""


def parse_module_address(leaf_id: str) -> ModuleAddress:
    """The bus address in a ``leafId`` token.

    Raises :class:`AmpioProtocolError` for any string that is not a
    ``0_<macHex>_<sfId>_<subSfId>_<ioNo>`` token, the empty string included.
    """
    match = _LEAF_ID_RE.fullmatch(leaf_id)
    if match is None:
        raise LeafFault(
            f"The Ampio object catalogue carries the leafId {leaf_id!r}, "
            "which is not a 0_<macHex>_<sfId>_<subSfId>_<ioNo> token"
        )
    return ModuleAddress(
        mac=int(match.group(1), 16),
        channel=int(match.group(4)),
        sf_id=int(match.group(2)),
        sub_sf_id=int(match.group(3)),
    )


def _shared_columns(row: Mapping[str, Any]) -> ObjectMetadata:
    """The object-catalogue columns `data/devices` serves on every row."""
    return ObjectMetadata(
        id=_int_column(row, "id", _CATALOGUE),
        typ_komponentu=_text_column(row, "typ_komponentu", _CATALOGUE),
        interpretacja=_int_column(row, "interpretacja", _CATALOGUE),
        funkcja=_int_column(row, "funkcja", _CATALOGUE),
        leaf_id=_leaf_column(row),
        name=_nullable_text_column(row, "opis_menu", _CATALOGUE) or None,
        matter_device_type=to_int(_column(row, "type", _CATALOGUE)),
        format=_nullable_text_column(row, "format", _CATALOGUE),
    )


def parse_app_sync_devices(data: Mapping[str, Any]) -> list[ObjectMetadata]:
    """Every row of a `data/devices` reply, the object catalogue.

    The surface serves no `params`, `czas`, or `url` column, so nothing here
    reads one - `data/params_devices` carries the three on every tier.
    """
    return [_shared_columns(row) for row in require_rows(data, _CATALOGUE)]


def parse_devices(data: Mapping[str, Any]) -> list[AmpioModule]:
    """Parse a `devices` payload into a list of physical modules.

    Returned modules have `last_seen=None`; the caller preserves any existing
    `last_seen` from a prior discovery.
    """
    return [
        AmpioModule(
            id=_int_column(row, "id", _MODULE_LIST),
            mac=_int_column(row, "mac", _MODULE_LIST),
            mac_global=_int_column(row, "mac_global", _MODULE_LIST),
            nazwa_urzadzenia=_nullable_text_column(
                row, "nazwa_urzadzenia", _MODULE_LIST
            )
            or None,
            typ_urzadzenia=_int_column(row, "typ_urzadzenia", _MODULE_LIST),
            wersja_softu=_int_column(row, "wersja_softu", _MODULE_LIST),
            wersja_pcb=_int_column(row, "wersja_pcb", _MODULE_LIST),
        )
        for row in require_rows(data, _MODULE_LIST)
    ]


@dataclass(slots=True, frozen=True)
class ParamsEntry:
    """One object's row in the ``data/params_devices`` table."""

    # `params` bitfield; see `HIDDEN_FLAG` and its neighbors in models.py.
    # `params` can exceed 32 bits (the matter-exposed flag is bit 37),
    # which Python ints handle natively.
    params: int
    # `czas` column as served, in 10 ms ticks; `AmpioObject.pulse_ms` reads
    # it on the kinds a timed write pulses.
    czas: int
    # Designer's "Unit" column, verbatim. `AmpioObject.unit` reads it.
    url: str


def parse_params_devices(data: Mapping[str, Any]) -> dict[int, ParamsEntry]:
    """Parse a `data/params_devices` payload into per-object config facts.

    Every row must carry `params`, `czas`, and `url`, and a row without one
    raises :class:`AmpioProtocolError`.
    """
    return {
        _int_column(row, "id", _PARAMS_TABLE): ParamsEntry(
            params=_int_column(row, "params", _PARAMS_TABLE),
            czas=_int_column(row, "czas", _PARAMS_TABLE),
            url=_text_column(row, "url", _PARAMS_TABLE),
        )
        for row in require_rows(data, _PARAMS_TABLE)
    }


def parse_scenes(data: Mapping[str, Any]) -> list[AmpioScene]:
    """Parse a `data/scenes` payload into the scene catalogue.

    Each row carries its actions twice - `Actions` as the wire command strings
    and `Infos` as their structured form. Only the object ids are kept, since
    the M-SERV replays the actions itself when a scene is run. `parentId` is
    the room the scene is filed under, an id of the `groups` table, and -1
    means no room.
    """
    out: list[AmpioScene] = []
    for item in require_rows(data, _SCENES):
        group = _int_column(item, "parentId", _SCENES)
        out.append(
            AmpioScene(
                id=_int_column(item, "id", _SCENES),
                scene_name=_text_column(item, "sceneName", _SCENES),
                active=_int_column(item, "active", _SCENES) != 0,
                group_id=group if group >= 0 else None,
                object_ids=_scene_object_ids(item),
            )
        )
    return out


def _scene_object_ids(row: Mapping[str, Any]) -> frozenset[int]:
    """The object ids of one scene row, out of its ``Infos`` annex.

    The annex is the structured form of the row's actions, one entry per
    action, and its ids are what relate a scene to its objects. A shape that
    carries none of them would read as a scene that touches nothing.
    """
    infos = _column(row, "Infos", _SCENES)
    if not isinstance(infos, list):
        raise AmpioProtocolError(f"An Ampio {_SCENES} row's `Infos` is not an array")
    out: set[int] = set()
    for entry in infos:
        if not isinstance(entry, dict):
            raise AmpioProtocolError(
                f"An Ampio {_SCENES} row's `Infos` entry is not a JSON object"
            )
        out.add(_int_column(entry, "id", f"{_SCENES} `Infos` entry"))
    return frozenset(out)


def parse_groups(data: Mapping[str, Any]) -> dict[int, str]:
    """``{group_id: name}`` from a `data/groups` reply, the room tree.

    Every row carries an id and a name. The name becomes a consumer's area,
    so an empty one names no room and is refused.
    """
    out: dict[int, str] = {}
    for row in require_rows(data, _GROUPS):
        name = _text_column(row, "opis_menu", _GROUPS)
        if not name:
            raise AmpioProtocolError(
                f"The `opis_menu` column of an Ampio {_GROUPS} row is empty"
            )
        out[_int_column(row, "id", _GROUPS)] = name
    return out


def parse_group_devices(data: Mapping[str, Any]) -> list[tuple[int, int]]:
    """``(object_id, group_id)`` per row of a `data/group_devices` reply.

    The order is the reply's own.
    """
    return [
        (
            _int_column(row, "id_obiektu", _MEMBERSHIP),
            _int_column(row, "id_grupy", _MEMBERSHIP),
        )
        for row in require_rows(data, _MEMBERSHIP)
    ]


def parse_rooms(
    group_names: Mapping[int, str], membership: Sequence[tuple[int, int]]
) -> dict[int, str]:
    """Join the two room tables into ``{ampio_object_id: room_name}``.

    An object in several groups takes the first room of the membership
    reply, because the join table marks no primary group. An object
    takes the first membership row whose group the names table lists. An
    object with no such row has no room.
    """
    room_map: dict[int, str] = {}
    for oid, gid in membership:
        if oid in room_map:
            continue
        name = group_names.get(gid)
        if name is not None:
            room_map[oid] = name
    return room_map


def parse_locations(data: Mapping[str, Any]) -> dict[int, str]:
    """``{location_id: name}`` from a `config/locations` reply.

    The name table behind the Designer's "Lokalizacja" dropdown. Every row
    carries an id and a name, and a pointer into a row with neither would
    read as an unassigned location.
    """
    out: dict[int, str] = {}
    for row in require_rows(data, _LOCATIONS):
        name = _text_column(row, "opis_menu", _LOCATIONS)
        if not name:
            raise AmpioProtocolError(
                f"The `opis_menu` column of an Ampio {_LOCATIONS} row is empty"
            )
        out[_int_column(row, "id", _LOCATIONS)] = name
    return out


@dataclass(slots=True, frozen=True)
class OutputDescription:
    """One per-output entry of a module's CAN-resident description record."""

    desc_type: int  # description class (OUTPUTS=12, ROLLER=26, ...)
    out_no: int  # output index within the class
    # pointer into the locations name table; 0 or 0x3FFF (the cleared form)
    # = unassigned
    out_loc: int
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
    # `{function id: channel count}` from the record's capability blob.
    # Empty when the device advertises nothing or the blob is unreadable.
    capabilities: Mapping[int, int] = field(default_factory=dict)
    # The record's raw settings blob, undecoded: what it means depends on
    # the module's hardware, which the record does not carry.
    params: bytes = b""


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


def _decode_blob(raw: object) -> bytes:
    """A base64 record field as bytes; anything unreadable reads empty."""
    if not isinstance(raw, str) or not raw:
        return b""
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return b""


def parse_capability_blob(raw: object) -> dict[int, int]:
    """A record's ``supportedFunctions`` blob as ``{function id: count}``.

    The blob is base64 of 2-byte pairs, the function id then its channel
    count. Anything unreadable - absent, not base64, an odd length -
    reads empty, so a device keeps its record either way. A repeated id
    takes its last pair.
    """
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return {}
    if len(blob) % 2:
        return {}
    return {blob[i]: blob[i + 1] for i in range(0, len(blob), 2)}


def parse_device_list(payload: str) -> tuple[DeviceRecord, ...] | None:
    """Every device of a ``device_api/from/list`` reply, with its record.

    None when the payload is not a JSON object with a ``devices`` list. A
    device without a ``descriptions`` field reads empty - no descriptions
    written. A device whose ids do not parse or whose ``descriptions``
    blob is unreadable is left out, so it counts as unlisted.
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
        out.append(
            DeviceRecord(
                mac=mac,
                mac_global=mac_global,
                entries=entries,
                capabilities=parse_capability_blob(item.get("supportedFunctions")),
                params=_decode_blob(item.get("params")),
            )
        )
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
) -> dict[int, DesignerRecord]:
    """Join each object to its module's description entry.

    The key is ``(DESC_TYPE_BY_KIND[typ_komponentu], address.channel)``
    within the module record of ``address.mac``.
    ``out_loc`` 0 or 16383 reads unassigned and ``out_type`` 0 untagged,
    so none produces a value. A ``desc`` that is empty or the ``.``
    placeholder reads as None, like the other two fields.
    """
    entries_by_key = {
        mac: {(e.desc_type, e.out_no): e for e in entries}
        for mac, entries in descriptions_by_mac.items()
    }
    out: dict[int, DesignerRecord] = {}
    for obj in objects.values():
        desc_type = DESC_TYPE_BY_KIND.get(obj.typ_komponentu or "")
        if desc_type is None:
            continue
        mac, out_no = obj.address.mac, obj.address.channel
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


# The `(typ_urzadzenia, wersja_pcb)` pairs whose panel params layout is
# live-proven. An unlisted pair resolves nothing. Extend only with a pair
# read off real hardware.
PANEL_PARAMS_LAYOUTS: frozenset[tuple[int, int]] = frozenset(
    {
        (8, 4),  # M-DOT-4
        (9, 5),  # M-DOT-18
        (11, 12),  # M-DOT-9
        (33, 5),  # M-DOT-2
    }
)


def parse_panel_settings(blob: bytes, fields: int) -> PanelSettings | None:
    """The panel section of a params blob, for a panel with ``fields`` fields.

    None when ``fields`` is not positive or the blob is too short to hold
    the whole section - a truncated blob must not read as confident values.
    docs/description-records.md carries the offsets.
    """
    mask_len = -(-fields // 8)  # bytes needed for one bit per field
    light = 7
    beep = light + fields
    sound = beep + 1
    backlight = sound + mask_len
    multitouch = backlight + mask_len
    send_count = multitouch + mask_len
    dimming = send_count + 1
    if fields <= 0 or len(blob) < dimming + 2:
        return None

    def bits(start: int) -> tuple[bool, ...]:
        mask = int.from_bytes(blob[start : start + mask_len], "little")
        return tuple(bool(mask >> bit & 1) for bit in range(fields))

    return PanelSettings(
        touch_field_color=(blob[0], blob[1], blob[2], blob[3]),
        status_color=(blob[4], blob[5], blob[6]),
        light_signal=tuple(blob[light:beep]),
        beep_time=blob[beep],
        sound_signal=bits(sound),
        backlight_active=bits(backlight),
        multitouch_lock=bits(multitouch),
        multitouch_send_count=bool(blob[send_count]),
        dim_after_s=blob[dimming],
        dim_brightness=blob[dimming + 1],
    )


def resolve_panel_settings(
    params_by_mac: Mapping[int, bytes],
    capabilities_by_mac: Mapping[int, Mapping[int, int]],
    hardware_by_mac: Mapping[int, tuple[int | None, int | None]],
) -> dict[int, PanelSettings]:
    """The panel settings of every module whose layout is proven, by mac.

    A module resolves only when its ``(typ_urzadzenia, wersja_pcb)`` pair
    is a proven layout and it advertises a backlight channel count - that
    count is the number of touch fields.
    """
    out: dict[int, PanelSettings] = {}
    for mac, blob in params_by_mac.items():
        typ, pcb = hardware_by_mac.get(mac, (None, None))
        if typ is None or pcb is None or (typ, pcb) not in PANEL_PARAMS_LAYOUTS:
            continue
        fields = capabilities_by_mac.get(mac, {}).get(ModuleFunction.BACKLIGHT_RGBW)
        if fields is None:
            continue
        settings = parse_panel_settings(blob, fields)
        if settings is not None:
            out[mac] = settings
    return out


@dataclass(slots=True, frozen=True)
class _CoverLayout:
    """Where one board keeps its roller section, and how wide a channel is.

    The stride must leave every field index at `8N + t` and below inside
    the section, since `parse_cover_parameters` reads them with no bounds
    check.
    """

    offset: int
    channels: int
    stride: int


# The `(typ_urzadzenia, wersja_pcb)` pairs whose roller params layout is
# live-proven. The Designer keys the layout by the same pair, and the
# boards differ in all three numbers. A stride of 10 ends the section
# before the two motor start lags, which that board does not hold.
# Reading one board with another's layout would produce confident wrong
# values, so an unlisted pair resolves nothing. Extend only with a pair
# read off real hardware.
COVER_PARAMS_LAYOUTS: Mapping[tuple[int, int], _CoverLayout] = {
    (3, 8): _CoverLayout(offset=5, channels=4, stride=10),  # M-ROL-4s
    (24, 11): _CoverLayout(offset=33, channels=1, stride=12),  # M-REL-2
}


def parse_cover_parameters(
    blob: bytes, layout: _CoverLayout
) -> tuple[CoverParameters, ...] | None:
    """One board's roller section, one entry per channel in channel order.

    None when the blob is too short to hold the whole section - a
    truncated blob must not read as confident values.
    docs/description-records.md carries the offsets.
    """
    count = layout.channels
    end = layout.offset + layout.stride * count
    if count <= 0 or len(blob) < end:
        return None
    section = blob[layout.offset : end]

    def u16(index: int) -> int:
        return section[index] | section[index + 1] << 8

    def lag(index: int) -> int | None:
        return section[index] * 10 if index < len(section) else None

    return tuple(
        CoverParameters(
            with_slats=bool(section[channel]),
            open_time_s=u16(count + 2 * channel),
            close_time_s=u16(3 * count + 2 * channel),
            calibration_percent=section[5 * count + channel],
            slat_time_ms=u16(6 * count + 2 * channel) * 10,
            reversal_lag_ms=section[8 * count + channel] * 10,
            start_lag_same_ms=lag(10 * count + channel),
            start_lag_other_ms=lag(11 * count + channel),
        )
        for channel in range(count)
    )


def resolve_cover_parameters(
    objects: Mapping[int, AmpioObject],
    params_by_mac: Mapping[int, bytes],
    capabilities_by_mac: Mapping[int, Mapping[int, int]],
    hardware_by_mac: Mapping[int, tuple[int | None, int | None]],
) -> dict[int, CoverParameters]:
    """Join each cover object to its channel's stored travel parameters.

    A module resolves only when its ``(typ_urzadzenia, wersja_pcb)`` pair
    is a proven layout. The channel count comes from that layout and not
    from the module: the four-channel board advertises no ``ROLLER``
    capability at all. Where a module does advertise one and its count
    disagrees with the layout, the module resolves nothing rather than
    guessing.

    The channel key matches ``resolve_designer``: ``address.channel``.
    """
    channels_by_mac: dict[int, tuple[CoverParameters, ...]] = {}
    for mac, blob in params_by_mac.items():
        typ, pcb = hardware_by_mac.get(mac, (None, None))
        if typ is None or pcb is None:
            continue
        layout = COVER_PARAMS_LAYOUTS.get((typ, pcb))
        if layout is None:
            continue
        advertised = capabilities_by_mac.get(mac, {}).get(ModuleFunction.ROLLER)
        if advertised is not None and advertised != layout.channels:
            continue
        channels = parse_cover_parameters(blob, layout)
        if channels is not None:
            channels_by_mac[mac] = channels

    out: dict[int, CoverParameters] = {}
    for obj in objects.values():
        if not joins_roller_records(obj.typ_komponentu):
            continue
        mac, channel = obj.address.mac, obj.address.channel
        channels = channels_by_mac.get(mac)
        if channels is None or not 0 <= channel < len(channels):
            continue
        out[obj.id] = channels[channel]
    return out


def resolve_module_capabilities(
    capabilities_by_mac: Mapping[int, Mapping[int, int]],
) -> dict[int, Mapping[int, int]]:
    """The capability map of every answering module, by mac.

    An empty map means the module advertised nothing or its
    ``supportedFunctions`` blob is absent or unreadable.
    """
    return dict(capabilities_by_mac)


def resolve_module_records(
    descriptions_by_mac: Mapping[int, tuple[OutputDescription, ...]],
    location_names: Mapping[int, str],
) -> dict[int, ModuleRecord]:
    """The DEVICE_NAME record entry of every answering module, by mac.

    A record without the entry reads an empty bundle - the module
    answered, so the emptiness is authoritative. The unassigned and
    placeholder sentinels read None, exactly as ``resolve_designer``
    reads them.
    """
    out: dict[int, ModuleRecord] = {}
    for mac, entries in descriptions_by_mac.items():
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
    version, so a non-str value there would raise instead of comparing. A
    value that is not a scalar reads as None, so a nested object never
    becomes text.
    """
    if value in (None, "") or not _is_scalar(value):
        return None
    return str(value)


def parse_server_info(data: Mapping[str, Any]) -> AmpioServerInfo:
    """Parse a server-info reply, keeping only the safe fields.

    The baseline server wraps the fields in a ``Results`` object and always
    reports two things: its ``mac``, the identity every consumer scopes a
    registry by, and ``userId``, the asking account. A reply missing any
    of the three is refused, so every :class:`AmpioServerInfo` carries a
    populated :pyattr:`AmpioServerInfo.server_key` and a readable
    :pyattr:`AmpioServerInfo.access_tier`.
    """
    results = data.get("Results")
    if not isinstance(results, dict):
        raise AmpioProtocolError(f"The Ampio {_INFO} reply carries no `Results` object")
    return AmpioServerInfo(
        mac=_int_column(results, "mac", _INFO),
        user_id=_int_column(results, "userId", _INFO),
        server_version=_to_str(results.get("serverVersion")),
        server_revision=_to_str(results.get("serverRevision")),
        mqtt_version=_to_str(results.get("mqttVersion")),
        local_ip=_to_str(results.get("local_ip")),
        device_id=_to_str(results.get("device_id")),
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


def redact_info_reply(data: Mapping[str, Any]) -> str:
    """The server-info reply with only its safelisted scalar values.

    Every other key is left out, and a safelisted key whose value is not a
    string, a number or null reads the redaction marker. A reply without a
    ``Results`` object is withheld outright.
    """
    results = data.get("Results")
    if not isinstance(results, dict):
        return REDACTED

    def _safe(fields: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value if _is_scalar(value) else REDACTED
            for key, value in fields.items()
            if key in _INFO_SAFE_KEYS
        }

    masked = _safe(data)
    masked["Results"] = _safe(results)
    return json.dumps(masked)


def _is_scalar(value: object) -> bool:
    """Whether a JSON value is a string, a number, a bool or null."""
    return value is None or isinstance(value, str | int | float)


def summarize_rows(data: Mapping[str, Any]) -> str:
    """Retain the row count, or withhold a reply that carries no rows."""
    try:
        rows = require_rows(data, "diagnostics")
    except AmpioProtocolError:
        return REDACTED
    return json.dumps({"row_count": len(rows)})


def parse_states_snapshot(data: Mapping[str, Any]) -> list[SnapshotEntry]:
    """Parse a bulk `data/states` snapshot.

    The reply lists the objects that hold a value, so every row carries
    its own `stan_json`.
    """
    return [
        SnapshotEntry(
            id=_int_column(row, "id", _SNAPSHOT),
            stan_json=_text_column(row, "stan_json", _SNAPSHOT),
        )
        for row in require_rows(data, _SNAPSHOT)
    ]


def _finite_float(raw: object) -> float | None:
    """A finite float from a wire value (string on the wire), else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return None
    try:
        parsed = float(raw)
    except (ValueError, OverflowError):
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

    The payload is a JSON object carrying the value and the M-SERV stamp it
    was reported at. The stamp is what orders one report against another, so
    a payload without it is refused rather than stamped with this process's
    own clock.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as err:
        raise AmpioProtocolError(
            f"The Ampio state push for object {oid} is not JSON"
        ) from err
    if not isinstance(data, dict):
        raise AmpioProtocolError(
            f"The Ampio state push for object {oid} is not a JSON object"
        )
    raw_on = _column(data, "on", _PUSH)
    if not isinstance(raw_on, (int, float)):
        raise AmpioProtocolError(
            f"The `on` stamp of the Ampio state push for object {oid} is not a number"
        )
    raw_state = _column(data, "state", _PUSH)
    if raw_state is None:
        raise AmpioProtocolError(
            f"The Ampio state push for object {oid} carries a null `state`"
        )
    return StateUpdate(
        id=oid,
        # Numeric `state` values arrive as int/float from JSON; the library
        # contract is text, so coerce here rather than at every consumer.
        state=str(raw_state),
        on_ms=raw_on,
        lammel=to_int(data.get("lammel")),
        block=to_int(data.get("block")),
        thermostat=_parse_thermostat(data),
    )


def _frame_byte(value: object) -> int | None:
    """One byte of a CAN frame as an int, or None outside 0-255."""
    byte = to_int(value)
    return byte if byte is not None and 0 <= byte <= 0xFF else None


def parse_diagnostics(payload: str) -> ModuleDiagnostics | None:
    """Parse a `b/4F` diagnostics frame into supply voltage and temperature.

    The frame is `{"d": [0xFE, 0x4F, voltage, temperature], "m": mac}`.
    Voltage is in 0.2 V steps; temperature is offset by 100 °C and reads 0 on
    the modules that carry no temperature sensor. Returns None when the payload
    is not a diagnostics frame or its voltage byte lies outside 0-255. A
    temperature byte outside 0-255 reads as no reading.
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
    voltage_byte = _frame_byte(frame[2])
    if voltage_byte is None:
        return None
    voltage = voltage_byte * 0.2
    raw_temp = _frame_byte(frame[3]) or 0 if len(frame) > 3 else 0
    return ModuleDiagnostics(
        supply_voltage=round(voltage, 1),
        temperature=float(raw_temp - 100) if raw_temp else None,
    )


def parse_color_temp_frame(
    payload: str, mac: int, function: str
) -> tuple[RawChannelEdge, ...] | None:
    """Decode a color-temperature broadcast into one edge per channel.

    The frame is `{"d": [0xFE, <function>, power, coldness, ...], "m": mac}`,
    with one byte pair per channel from offset 2. ``function`` fixes which
    channel the first pair carries. Each pair repacks to `power |
    coldness<<8`, the same u16 the per-object topic reports. Returns None
    when the payload is not a color-temperature frame, has an odd length, or
    carries a byte outside 0-255.
    """
    first_channel = CCT_FRAME_FUNCTIONS.get(function)
    if first_channel is None:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    frame = data.get("d")
    if not isinstance(frame, list) or len(frame) < 4 or len(frame) % 2:
        return None
    if frame[0] != 0xFE or frame[1] != int(function, 16):
        return None
    edges: list[RawChannelEdge] = []
    for index in range((len(frame) - 2) // 2):
        power = _frame_byte(frame[2 + 2 * index])
        coldness = _frame_byte(frame[2 + 2 * index + 1])
        if power is None or coldness is None:
            return None
        edges.append(
            RawChannelEdge(
                mac=mac,
                prefix=CCT_PREFIX,
                channel=first_channel + index,
                state=str(power | coldness << 8),
            )
        )
    return tuple(edges) if edges else None


def parse_stan_json(stan_json: str) -> StanJsonSeed:
    """Parse a `stan_json` blob into an initial state and server timestamp.

    Raises :class:`AmpioProtocolError` for a blob that is not a JSON object,
    lacks a numeric `on` stamp, or carries a null `state`.
    """
    try:
        data = json.loads(stan_json)
    except (ValueError, TypeError) as err:
        raise AmpioProtocolError(
            f"An Ampio {_SNAPSHOT} row's `stan_json` is not JSON"
        ) from err
    if not isinstance(data, dict):
        raise AmpioProtocolError(
            f"An Ampio {_SNAPSHOT} row's `stan_json` is not a JSON object"
        )
    raw_on = _column(data, "on", _SNAPSHOT)
    if not isinstance(raw_on, (int, float)):
        raise AmpioProtocolError(
            f"The `on` stamp of an Ampio {_SNAPSHOT} row is not a number"
        )
    raw_state = _column(data, "state", _SNAPSHOT)
    if raw_state is None:
        raise AmpioProtocolError(f"An Ampio {_SNAPSHOT} row carries a null `state`")
    return StanJsonSeed(
        state=str(raw_state),
        on_ms=raw_on,
        lammel=to_int(data.get("lammel")),
        block=to_int(data.get("block")),
        thermostat=_parse_thermostat(data),
    )


# --- Endpoint table --------------------------------------------------------
#
# One row per M-SERV request/response endpoint, and the row is the single
# source of truth: subscriptions, routing, discovery-completion signals,
# and retained payloads all derive from it. To add an endpoint: verify
# the wire shape live, add the row, give a fetchable endpoint a `parses=`
# parser (a state-mutating one gets an entry in `AmpioStore._handler_table()`,
# or in the `AdminStore` override for an admin-only reply, instead), and
# expose a `fetch_<name>()` awaiting `AmpioClient._fetch` - `fetch_scenes()`
# is the reference shape.
#
# A request publishes ``req_payload`` (a keyword, or "" for the dedicated
# ``states``/``info`` surfaces) to ``ampio/control/<user>/<req_surface>``;
# the reply lands on ``ampio/fromDB/<user>/<resp_surface>/<resp_leaf>``.


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One M-SERV request/response endpoint."""

    name: str
    req_surface: str  # control sub-topic: "config" | "states" | "info" | "data"
    req_payload: str  # request keyword, or "" for the states/info surfaces
    resp_surface: str  # fromDB sub-topic: "config" | "data"
    resp_leaf: str  # final response-topic segment
    # Part of the initial-discovery set awaited by connect() /
    # wait_for_initial_discovery().
    initial: bool = False
    # The one tier this endpoint answers for, or None for both. The M-SERV
    # serves the config surfaces to administrators only. The object
    # catalogue rides the data surface on every account.
    tier: AccessTier | None = None
    # The reply parser for a pure request/response endpoint. The dispatcher
    # runs it exactly once, and the parsed value is what a fetch returns. A
    # reply the parser refuses raises `AmpioProtocolError`, which neither
    # resolves a fetch nor latches discovery. None marks an endpoint whose
    # reply mutates state - its AmpioStore handler is the gate instead.
    parses: Callable[[Mapping[str, Any]], object] | None = None
    # Overrides the default row-count summary in diagnostics_snapshot().
    # The retained string must omit private content because a consumer's
    # key-based redactor cannot reach inside it.
    redacts: Callable[[Mapping[str, Any]], str] | None = None


ENDPOINTS: tuple[Endpoint, ...] = (
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
        "info", "info", "", "data", "info", initial=True, redacts=redact_info_reply
    ),
    # The object catalogue, served to every account: the objects in the
    # account's app-sync view, which on the reserved admin login is every
    # object in a room, and the account's own grants otherwise. Every reply
    # also carries the two system rows, and the store drops them by their
    # type.
    Endpoint(
        "data_devices",
        "data",
        "devices",
        "data",
        "devices",
        initial=True,
    ),
    # Per-object params bitfields for the catalogue. Not grant-filtered:
    # every account receives the full table.
    Endpoint(
        "params_devices",
        "data",
        "params_devices",
        "data",
        "params_devices",
        initial=True,
    ),
    Endpoint("groups", "data", "groups", "data", "groups", parses=parse_groups),
    Endpoint(
        "group_devices",
        "data",
        "group_devices",
        "data",
        "group_devices",
        parses=parse_group_devices,
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

# The endpoints each client class is served. The base client and the base
# store read the first. The admin client and the admin store read the second.
# `Endpoint.tier` is the wire fact both derive from.
BASE_ENDPOINTS: tuple[Endpoint, ...] = tuple(ep for ep in ENDPOINTS if ep.tier is None)
ADMIN_ENDPOINTS: tuple[Endpoint, ...] = ENDPOINTS


# The M-SERV software baseline this library is developed and live-tested
# against, as the server self-reports it on the info surface. This is the
# compatibility floor, not a promise about anything older: a lower (or
# missing) serverVersion logs a warning at discovery and behavior on such a
# server is undefined - the fix is upgrading the M-SERV. Only serverVersion
# is compared.
BASELINE_SERVER_VERSION = (1865,)


# --- Commands --------------------------------------------------------------
#
# Writes go to one control topic per account as plain text:
# ``/api/set/<object_id>/<verb>[/<arg>...]``. The verb vocabulary is the
# M-SERV's own HTTP API, re-exposed over MQTT; see docs/commands.md for
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


def notification_payload(message: str) -> str:
    """Build the payload that pushes a notification to the Ampio app.

    The message rides a path segment but needs no escaping: the payload is
    an MQTT string rather than an HTTP request line, and the M-SERV passes
    spaces, UTF-8 and a literal `%20` straight through to the app.
    """
    return f"/api/pushNotification/{message}"


def scene_payload(scene_id: int, verb: str) -> str:
    """Build a scene command payload; ``verb`` is run, off, or undo."""
    return f"/api/{verb}/scene/{scene_id}"


# `setRollerPos` takes a position and a lamella angle. 101 on either axis means
# "leave this one where it is", so one command can move either axis alone or
# both together.
KEEP_POSITION = 101


# The raw CAN write frame `<fn> F9 <value> <channel>`: 0xF9 is the set-u8
# command and the first byte the per-class function the Designer sends
# from its SF table. It is the ONLY write that reaches a classic panel's
# binary outputs (status LEDs) - the `/api` verbs and the per-channel
# `o/<ch>/cmd` form are silently dropped for those, while a relay module
# answers all three. docs/panel-writes.md ("Panel outputs") carries the live
# evidence.

# The first frame byte per leaf class, live-proven pairs only: binary
# outputs (sfId 257: relays and panel LEDs) take the generic 0x30, the
# open-collector output (sfId 67, M-INOC) takes 0x32. A module drops 0x30
# on a class-67 leaf. A class outside the table stays on `/api`.
RAW_OUTPUT_FUNCTION_BY_SF: dict[int, int] = {257: 0x30, 67: 0x32}

# The leaf class whose outputs report on the raw `a` prefix as a u8 value
# instead of the binary `o` prefix; the same 1-based channel numbering.
OC_OUTPUT_SF = 67


def raw_write_topic(mac: int) -> str:
    """The raw CAN write topic for one module, mac in lowercase hex."""
    return f"ampio/to/{mac:x}/raw"


def raw_output_payload(function: int, value: int, channel: int) -> str:
    """The set-output frame as the wire's ASCII hex form.

    ``function`` is the leaf class's first byte
    (:data:`RAW_OUTPUT_FUNCTION_BY_SF`). ``channel`` is the 0-based output
    index - ``AmpioObject.address.channel``, one below the 1-based raw
    state channel.
    """
    return f"{function:02x}f9{value:02x}{channel:02x}"


# The Designer's "test condition" button executes one module action at
# once: the `0c07` envelope, a trigger state byte, then the action. State
# 3 asserts and 0 releases, and the button sends 3 on press. Every action
# that uses this prefix asserts. The roller lock builds its own envelope,
# because its release sends state 0.
#
# An action starts with its destination in the high nibble and its
# function in the low one. A destination above one byte takes the escape
# form instead, `0xF0 | function` then the destination's low byte. Every
# time field counts 10 ms ticks and 16-bit fields are little-endian.
# docs/panel-writes.md carries the wire facts.
_ACTION_FRAME_PREFIX = "0c0703"
# Action destinations, each with its function in the low nibble.
_BACKLIGHT_ACTION = "50"  # per-field RGBW backlight
_STATUS_LIGHT_ACTION = "60"  # per-field RGB status indicator
_KEY_LOCK_ACTION = "f02f"  # destination 303, so the escape form
# The backlight and status light actions use the vendor's sub-function 1,
# the code the stored conditions on live modules carry. For the backlight, 1 and 2
# both set the resting color and neither outranks the other.
_ACTION_SUB_FUNCTION = "01"
# A panel reports at most 24 touch fields, so three mask bytes cover any
# of them. A module reads the width its own field count needs and ignores
# the rest, which is what lets a caller send the full width blind.
PANEL_MASK_MAX_BYTES = 3


def raw_buzzer_payload(on: bool, tone: int, ticks: int) -> str:
    """The simple buzzer action as the wire's ASCII hex form.

    ``on`` selects the ON sub-function, else OFF. ``ticks`` is the length
    in 10 ms ticks; 0 with ON latches the buzzer on.
    """
    return f"{_ACTION_FRAME_PREFIX}70{int(on):02x}{tone:02x}{ticks:02x}"


def raw_buzzer_pattern_payload(
    tone1: int, ticks1: int, tone2: int, ticks2: int, cycles: int, delay_ticks: int
) -> str:
    """The sequence buzzer action, sub-function ON, as ASCII hex.

    Tone 0 is a silent rest. ``cycles`` 0 repeats until another frame
    replaces the sequence.
    """
    return (
        f"{_ACTION_FRAME_PREFIX}7101"
        f"{delay_ticks & 0xFF:02x}{delay_ticks >> 8:02x}"
        f"{tone1:02x}00{ticks1 & 0xFF:02x}{ticks1 >> 8:02x}"
        f"{tone2:02x}00{ticks2 & 0xFF:02x}{ticks2 >> 8:02x}"
        f"{cycles:02x}"
    )


# The stop pair. A one-cycle silent sequence replaces a running pattern
# within 100 ms; the simple OFF (tone at the Designer default 6) ends a
# plain beep or a latched ON, which a sequence step would otherwise
# re-assert.
RAW_BUZZER_SILENCE = raw_buzzer_pattern_payload(0, 1, 0, 0, 1, 0)
RAW_BUZZER_OFF = raw_buzzer_payload(False, 6, 0)


def panel_field_mask(fields: Sequence[int] | None, width: int) -> str:
    """The touch field mask of a panel action, as ASCII hex.

    One bit per field, least significant first, so field 1 is bit 0.
    None selects every field.
    """
    if fields is None:
        return "ff" * width
    mask = 0
    for number in fields:
        mask |= 1 << (number - 1)
    return mask.to_bytes(width, "little").hex()


def raw_backlight_payload(
    red: int, green: int, blue: int, white: int, mask: str
) -> str:
    """The per-field backlight color action as ASCII hex."""
    return (
        f"{_ACTION_FRAME_PREFIX}{_BACKLIGHT_ACTION}{_ACTION_SUB_FUNCTION}"
        f"{red:02x}{green:02x}{blue:02x}{white:02x}{mask}"
    )


def raw_status_light_payload(red: int, green: int, blue: int, mask: str) -> str:
    """The per-field status indicator color action as ASCII hex."""
    return (
        f"{_ACTION_FRAME_PREFIX}{_STATUS_LIGHT_ACTION}{_ACTION_SUB_FUNCTION}"
        f"{red:02x}{green:02x}{blue:02x}{mask}"
    )


# The roller lock, three of the eleven sub-functions the roller time
# function carries. The Designer dropdown index is the wire byte, and only
# these touch the lock (#223). Sub-function 8 blocks both directions at
# once and needs no wrapper, because two calls say the same thing.
ROLLER_BLOCK_CLOSING = 9
ROLLER_BLOCK_OPENING = 10
# The roller action's destination. A module whose own action table lists
# a roller entry takes the destination from there. Every module measured
# so far carries no such entry and falls through to the special
# function's default, 261, which is above one byte and so travels in the
# escape form. docs/panel-writes.md records what the wire answered.
_ROLLER_ACTION_DST = 261
# The time function of the roller action, the one the lock rides.
_ROLLER_ACTION_FUNC = 0
# The delay and time fields the time function ends with. Designer hides
# both inputs for the lock sub-functions, so a lock never expires.
_ROLLER_LOCK_UNUSED_TAIL = "00000000"


def raw_roller_lock_payload(
    sub_function: int, channel: int, channels: int, *, assert_lock: bool
) -> str:
    """The roller lock action as the wire's ASCII hex form.

    ``channels`` is the module's roller channel count, which sizes the
    mask: one bit per channel, lowest channel first. ``assert_lock``
    False sends the release state, which clears that sub-function's bit
    and leaves the other one alone.
    """
    mask = bytearray((channels + 7) // 8)
    mask[channel >> 3] |= 1 << (channel & 7)
    return (
        f"0c07{3 if assert_lock else 0:02x}"
        f"{0xF0 | _ROLLER_ACTION_FUNC:02x}{_ROLLER_ACTION_DST & 0xFF:02x}"
        f"{sub_function:02x}{mask.hex()}{_ROLLER_LOCK_UNUSED_TAIL}"
    )


def raw_key_lock_payload(on: bool, ticks: int) -> str:
    """The touch lock action as ASCII hex.

    ``ticks`` is how long the lock holds, in 10 ms ticks. Zero is a lock
    of zero length, not a latch, so the module beeps and a touch works at
    once. ``on`` False releases the lock immediately and ignores the time.
    """
    return (
        f"{_ACTION_FRAME_PREFIX}{_KEY_LOCK_ACTION}{int(on):02x}"
        f"{ticks & 0xFF:02x}{ticks >> 8:02x}"
    )


# Module identify, the Designer's "Identify device" button: `[0x7E, flag]`
# addressed to the module. docs/panel-writes.md ("Module identify") carries
# the wire facts.
RAW_IDENTIFY_ON = "7e01"
RAW_IDENTIFY_OFF = "7e00"


def request_topic(ep: Endpoint, user: str) -> str:
    """Control topic an endpoint's request keyword is published to."""
    return f"ampio/control/{user}/{ep.req_surface}"


def response_topic(ep: Endpoint, user: str) -> str:
    """fromDB topic an endpoint's reply arrives on."""
    return f"ampio/fromDB/{user}/{ep.resp_surface}/{ep.resp_leaf}"


def ob_state_wildcard(user: str) -> str:
    """Wildcard for all object state topics for an account."""
    return f"ampio/fromDB/{user}/ob/+/state"


# The app-sync tables the M-SERV pushes into every account namespace on a
# Designer save, rewriting their retained `md5/<keyword>` digest with them.
# The admin client uses a changed digest to re-request the module list,
# which is never pushed (docs/discovery-flow.md).
CATALOGUE_DIGEST_KEYWORDS = ("devices", "params_devices")


def md5_topic(user: str, keyword: str) -> str:
    """Retained topic holding the MD5 of an account's app-sync table reply."""
    return f"ampio/fromDB/{user}/md5/{keyword}"


ACCOUNT_PLACEHOLDER = "<account>"

# The two trees whose third segment is the connecting account: the
# `control` requests and the `fromDB` replies are both namespaced by it.
_ACCOUNT_TREES = ("control", "fromDB")


def account_free_topic(topic: str) -> str:
    """`topic` with its account segment replaced by the placeholder.

    A diagnostics entry keyed on a topic names the surface that failed, and
    the account segment inside that key is a credential no key-based
    redactor can reach. The masked form names the surface just as well. The
    global `ampio/from` tree carries no account and returns unchanged.
    """
    parts = topic.split("/")
    if len(parts) > 2 and parts[0] == "ampio" and parts[1] in _ACCOUNT_TREES:
        parts[2] = ACCOUNT_PLACEHOLDER
        return "/".join(parts)
    return topic


def account_free_text(text: str, username: str, host: str) -> str:
    """`text` with the account and the broker host masked.

    An error message can name the account's own topic or the host it
    failed to reach. Each account segment after `ampio/control/` or
    `ampio/fromDB/` reads as the topic placeholder. The host reads as the
    redaction marker where it stands as a whole token, in any letter case,
    outside a topic and a placeholder.
    """
    trees = "|".join(map(re.escape, _ACCOUNT_TREES))
    text = re.sub(
        rf"(ampio/(?:{trees})/){re.escape(username)}(?![\w-])",
        lambda match: match[1] + ACCOUNT_PLACEHOLDER,
        text,
    )
    if not host:
        return text
    # A colon ends a host name before its port, but it continues an IPv6
    # address, so an IPv6 host must not match the start of a longer one.
    ipv6 = ":" in host
    before = r"(?<![\w.\-/<:])" if ipv6 else r"(?<![\w.\-/<])"
    after = r"(?![\w-]|\.[\w-]|:[0-9a-fA-F])" if ipv6 else r"(?![\w-]|\.[\w-])"
    return re.sub(before + re.escape(host) + after, REDACTED, text, flags=re.IGNORECASE)


# The raw `ampio/from/<MAC>/...` tree: global (not user-namespaced), retained,
# admin-only. docs/raw-channel-bridge.md is the home for which prefixes are
# subscribed and bridged, and which stay on the per-object topic.
RAW_INPUT_WILDCARDS = ("ampio/from/+/state/f/+", "ampio/from/+/state/i/+")

# Binary output channels, bridged for `przekaznik` objects. A touch
# panel's status LEDs have no other retained surface, and every module's
# binary outputs share the channel shape (docs/raw-channel-bridge.md).
RAW_OUTPUT_WILDCARD = "ampio/from/+/state/o/+"
# Analog output channels, bridged for the `przekaznik` objects on an
# open-collector leaf (class 67): the module reports those as a u8 on `a`,
# and the object topic never echoes them on any write path.
RAW_ANALOG_WILDCARD = "ampio/from/+/state/a/+"

# Per-module diagnostics broadcasts (CAN supply voltage, own temperature).
RAW_DIAGNOSTICS_WILDCARD = "ampio/from/+/b/4F"

# The color-temperature (`ledww`) broadcast. The module reports its CCT
# channels on two function bytes, three channels each, as `(power, coldness)`
# byte pairs from offset 2. The pair repacks to the same u16 the per-object
# topic carries, so a channel bridges as an ordinary edge under its own
# prefix. This is the only retained surface for the axes: the M-SERV bridges
# no `state/<prefix>/<n>` leaf for the type (docs/raw-channel-bridge.md).
CCT_PREFIX = "ww"
CCT_CHANNELS_PER_FRAME = 3
# Function byte -> the 1-based channel its first pair carries.
CCT_FRAME_FUNCTIONS: dict[str, int] = {"62": 1, "63": 4}
RAW_COLOR_TEMP_WILDCARDS = tuple(
    f"ampio/from/+/b/{function}" for function in CCT_FRAME_FUNCTIONS
)

# Bus events (1-65535); receiving rides the admin-only raw tree, raising goes
# to the command surface - the rights model is in docs/bus-events.md.
RAW_EVENT_WILDCARD = "ampio/from/+/event"


# The admin-only device_api tree. One `list` request returns every
# module's CAN-resident record in a single reply, the M-SERV's own
# included, each device tagged with both of its ids. The per-module
# get_data pair is keyed by the factory id and answers nothing on an
# override mac - docs/identity.md.
DEVICE_API_LIST_REQUEST = "device_api/to/list"
DEVICE_API_LIST_PAYLOAD = b"0"
DEVICE_API_LIST_TOPIC = "device_api/from/list"


# The description class the Designer gives a roller channel. Named so the
# cover decode derives its kind gate from this table instead of repeating
# the kind names.
ROLLER_DESC_TYPE = 26

# typ_komponentu -> description class (descType), live-proven pairs only
# (docs/description-records.md): an unlisted kind resolves no Designer record.
DESC_TYPE_BY_KIND: dict[str, int] = {
    "przekaznik": 12,  # OUTPUTS
    "roleta_procenty": ROLLER_DESC_TYPE,
    "roleta_lamelki": ROLLER_DESC_TYPE,
    "led": 16,  # OUT_OC_U8
    "rgbw": 34,  # RGBW output class; no symbolic name in the recovered enum
    "ledww": 81,  # LED_WW_CNT, the same id as the sub-function
    "flaga": 6,  # FLAG_BIN
}


def joins_roller_records(typ_komponentu: str | None) -> bool:
    """Whether a component kind joins the roller description class.

    The gate for every roller-only fact: the cover decode reads it to know
    which objects a params blob can join, and the store reads it to know
    which objects may keep what that decode produced.
    """
    return DESC_TYPE_BY_KIND.get(typ_komponentu or "") == ROLLER_DESC_TYPE


# --- topic routing ---------------------------------------------------------


@dataclass(slots=True, frozen=True)
class EndpointReply:
    """A reply on a request/response endpoint, payload unparsed.

    The endpoint's ``parses`` gate or, when it has none, the store's handler
    decides how the reply applies. The router only identifies the endpoint.
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
class ColorTempFrame:
    """One color-temperature broadcast, fanned out to one edge per channel.

    The frame carries up to three channels at once, so it resolves to a
    tuple rather than the single edge a `state/<prefix>/<n>` topic yields.
    Each edge is an ordinary `RawChannelEdge`, which lets the store apply it
    through the same path every other bridged channel takes.
    """

    edges: tuple[RawChannelEdge, ...]


@dataclass(slots=True, frozen=True)
class DiagnosticsReport:
    """A module's parsed `b/4F` health broadcast with its sender mac."""

    mac: int
    diagnostics: ModuleDiagnostics


@dataclass(slots=True, frozen=True)
class DeviceList:
    """Every device's parsed record from a device_api list reply."""

    devices: tuple[DeviceRecord, ...]


@dataclass(slots=True, frozen=True)
class CatalogueDigest:
    """The retained MD5 of one app-sync table, as the M-SERV last published it."""

    keyword: str
    digest: str


# Everything one MQTT message can classify into. `BusEventRaised` is the
# public event class itself - for bus events the wire message IS the event.
Inbound = (
    EndpointReply
    | StateUpdate
    | RawChannelEdge
    | ColorTempFrame
    | DiagnosticsReport
    | DeviceList
    | CatalogueDigest
    | BusEventRaised
)


class Router:
    """Classifies one MQTT message into a typed inbound message, or None.

    The home of inbound topic classification: anything unroutable returns
    None, and the client dispatches typed messages without inspecting a
    topic. ``endpoints`` is the tier's served subset, so a reply topic
    outside it is unroutable like any other unknown shape. Endpoint reply
    and per-object state topics are namespaced by the connecting account
    (hence ``user``); the raw ``ampio/from`` tree is global.

    The admin-only shapes (the raw tree, the digests, the device list) are
    routed for the admin client alone, so a router built with ``admin=False``
    returns None for a digest, the device list, and every raw topic.
    """

    __slots__ = ("_admin", "_by_response", "_user")

    def __init__(
        self, user: str, endpoints: tuple[Endpoint, ...], *, admin: bool = False
    ) -> None:
        self._user = user
        self._admin = admin
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
        if not self._admin:
            return None
        if (
            len(parts) == 5
            and parts[0] == "ampio"
            and parts[1] == "fromDB"
            and parts[2] == self._user
            and parts[3] == "md5"
            and parts[4] in CATALOGUE_DIGEST_KEYWORDS
        ):
            digest = payload.strip()
            # An empty payload is a retained clear, not a digest.
            return CatalogueDigest(keyword=parts[4], digest=digest) if digest else None
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
        if len(parts) == 5 and parts[3] == "b" and parts[4] in CCT_FRAME_FUNCTIONS:
            edges = parse_color_temp_frame(payload, mac, parts[4])
            return None if edges is None else ColorTempFrame(edges=edges)
        if len(parts) == 4 and parts[3] == "event":
            number = to_int(payload.strip())
            return (
                None if number is None else BusEventRaised(event_number=number, mac=mac)
            )
        return None
