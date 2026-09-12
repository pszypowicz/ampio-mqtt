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
  digests:   ampio/fromDB/<user>/md5/<table> (retained) = MD5 of an app-sync
             table's reply, rewritten by the M-SERV on a Designer save

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
from dataclasses import dataclass, field
from functools import partial
from typing import Any, cast

from .errors import AmpioProtocolError
from .events import BusEventRaised
from .models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    DesignerRecord,
    ModuleFunction,
    ModuleRecord,
    PanelSettings,
    ThermostatState,
)


@dataclass(slots=True)
class ObjectMetadata:
    """One object-catalogue row, in the columns both surfaces serve."""

    id: int
    id_urzadzenia: int  # physical module
    typ_komponentu: str
    interpretacja: int
    funkcja: int  # physical channel index within the module
    leaf_id: str  # `leafId`; empty for system objects, and after a Matter uncheck
    opis_menu: str | None  # empty reads None: the object carries no name
    # `type` column: the Matter device type ID assigned in Designer, carried
    # as a decimal string on the wire ("256" = 0x0100 On/Off Light). Empty or
    # null when the object has no tag - both read as None.
    # docs/description-records.md holds the vocabulary.
    matter_device_type: int | None
    # Designer's "String format" column (a printf conversion, optionally
    # followed by a unit), verbatim. Empty when unset. `AmpioObject.unit`
    # and `AmpioObject.decimals` read it.
    format: str


@dataclass(slots=True)
class AdminObjectMetadata:
    """A `config/devicesDetails` row: the shared columns plus three more.

    The app-sync catalogue serves none of the three, and
    `data/params_devices` is the app-sync tier's source for them. Which
    surface answers follows from the account tier, so each of these facts
    has exactly one source per tier (docs/account-tiers.md).
    """

    shared: ObjectMetadata
    # `params` bitfield; bit 4 = hidden/stub, bit 37 = matter-exposed.
    params: int
    # `czas` column as served, in 10 ms ticks; `AmpioObject.pulse_ms` reads
    # it by component type.
    czas: int
    # Designer's "Unit" column, verbatim. `AmpioObject.unit` reads it.
    url: str


@dataclass(slots=True)
class SnapshotEntry:
    """One object's entry in a bulk `data/states` snapshot.

    The snapshot lists the objects that hold a value, so every row carries
    its `stan_json`. An object with no value is absent from the reply.
    """

    id: int
    stan_json: str


@dataclass(slots=True)
class StateUpdate:
    """A live state push for a single object."""

    id: int
    state: str
    on_ms: int | float | None
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

    state: str | None
    on_ms: int | float | None
    lammel: int | None
    # Roller lock bits, present only on cover rows.
    block: int | None = None
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


def require_rows(payload: str, surface: str) -> list[dict[str, Any]]:
    """The rows of a ``{"List": [...]}`` reply.

    The M-SERV is the only expected publisher on these topics, but nothing on
    the broker enforces that, and a reply of the wrong shape must not reach the
    row loops - they index and attribute-access every row. Every such reply
    raises :class:`AmpioProtocolError` instead, because no caller can tell a
    tolerated malformed reply from an empty one.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as err:
        raise AmpioProtocolError(f"The Ampio {surface} reply is not JSON") from err
    if not isinstance(data, dict):
        raise AmpioProtocolError(f"The Ampio {surface} reply is not a JSON object")
    rows = data.get("List")
    if not isinstance(rows, list):
        raise AmpioProtocolError(f"The Ampio {surface} reply carries no `List` array")
    if not all(isinstance(row, dict) for row in rows):
        raise AmpioProtocolError(f"An Ampio {surface} row is not a JSON object")
    return cast("list[dict[str, Any]]", rows)


def _rows_parser(surface: str) -> Callable[[str], list[dict[str, Any]]]:
    """An endpoint parser handing a reply's rows to the caller, strictly.

    For a table whose row shape this library has not pinned live: the
    envelope is checked, the rows pass through, and the consumer's own join
    reads what it needs.
    """
    return partial(require_rows, surface=surface)


def to_int(value: Any) -> int | None:
    """Int coercion for a column whose absence is a value, None on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
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

    The M-SERV writes null in place of an empty string on some rows of
    ``leafId``, ``opis_menu``, and ``format``. The column must still be
    there: an absent one is a different reply shape.
    """
    value = _column(row, column, surface)
    return value if isinstance(value, str) else ""


# The surface names the protocol errors speak, one per reply shape.
_CATALOGUE = "object catalogue"
_MODULE_LIST = "module list"
_PARAMS_TABLE = "params_devices table"
_SNAPSHOT = "states snapshot"
_SCENES = "scene catalogue"
_LOCATIONS = "locations table"
_INFO = "server info"


def _shared_columns(row: Mapping[str, Any]) -> ObjectMetadata:
    """The object-catalogue columns both surfaces serve on every row.

    ``leafId`` holds an empty string for a system object and for one whose
    Matter box is unchecked in Designer. Otherwise it is a short
    underscored token like ``0_cb8f_76_0_0``, which the Designer reads as
    ``macGroup``, ``mac``, ``sfId``, ``subSfId``, and ``ioNo``. The parse
    keeps the raw string.
    """
    return ObjectMetadata(
        id=_int_column(row, "id", _CATALOGUE),
        id_urzadzenia=_int_column(row, "id_urzadzenia", _CATALOGUE),
        typ_komponentu=_text_column(row, "typ_komponentu", _CATALOGUE),
        interpretacja=_int_column(row, "interpretacja", _CATALOGUE),
        funkcja=_int_column(row, "funkcja", _CATALOGUE),
        leaf_id=_nullable_text_column(row, "leafId", _CATALOGUE),
        opis_menu=_nullable_text_column(row, "opis_menu", _CATALOGUE) or None,
        matter_device_type=to_int(_column(row, "type", _CATALOGUE)),
        format=_nullable_text_column(row, "format", _CATALOGUE),
    )


def parse_details(payload: str) -> list[AdminObjectMetadata]:
    """Every row of a `config/devicesDetails` reply, the admin catalogue.

    An empty list is a valid reply that lists nothing. `params` can exceed
    32 bits (the matter-exposed flag is bit 37), which Python ints handle
    natively.
    """
    return [
        AdminObjectMetadata(
            shared=_shared_columns(row),
            params=_int_column(row, "params", _CATALOGUE),
            czas=_int_column(row, "czas", _CATALOGUE),
            url=_text_column(row, "url", _CATALOGUE),
        )
        for row in require_rows(payload, _CATALOGUE)
    ]


def parse_app_sync_devices(payload: str) -> list[ObjectMetadata]:
    """Every row of a `data/devices` reply, the app-sync catalogue.

    The rows are the objects the account was granted in the Ampio app. The
    surface serves no `params`, `czas`, or `url` column, so nothing here
    reads one - `data/params_devices` is this tier's source for the three.
    """
    return [_shared_columns(row) for row in require_rows(payload, _CATALOGUE)]


def parse_devices(payload: str) -> list[AmpioModule]:
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
        for row in require_rows(payload, _MODULE_LIST)
    ]


@dataclass(slots=True, frozen=True)
class ParamsEntry:
    """One object's row in the ``data/params_devices`` table."""

    params: int
    czas: int
    url: str


def parse_params_devices(payload: str) -> dict[int, ParamsEntry]:
    """Parse a `data/params_devices` payload into per-object config facts.

    The table covers the full object catalogue regardless of the account's
    grants, so it carries a row for every object an app-sync catalogue can
    list, and every row carries all three columns.
    """
    return {
        _int_column(row, "id", _PARAMS_TABLE): ParamsEntry(
            params=_int_column(row, "params", _PARAMS_TABLE),
            czas=_int_column(row, "czas", _PARAMS_TABLE),
            url=_text_column(row, "url", _PARAMS_TABLE),
        )
        for row in require_rows(payload, _PARAMS_TABLE)
    }


def parse_scenes(payload: str) -> list[AmpioScene]:
    """Parse a `data/scenes` payload into the scene catalogue.

    Each row carries its actions twice - `Actions` as the wire command strings
    and `Infos` as their structured form. Only the object ids are kept, since
    the M-SERV replays the actions itself when a scene is run.
    """
    out: list[AmpioScene] = []
    for item in require_rows(payload, _SCENES):
        sid = to_int(item.get("id"))
        if sid is None:
            continue
        parent = to_int(item.get("parentId"))
        # Malformed row fields degrade instead of hiding the scene: it is
        # real and runnable (the M-SERV replays its actions server-side).
        # This row shape is not pinned live, so only the envelope is
        # strict. A row without `active` reads enabled, the state the app
        # creates.
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


def parse_locations(payload: str) -> dict[int, str]:
    """``{location_id: name}`` from a `config/locations` reply.

    The name table behind the Designer's "Lokalizacja" dropdown. This row
    shape is not pinned live, so only the envelope is strict: a row with a
    missing id or an empty name is skipped.
    """
    out: dict[int, str] = {}
    for row in require_rows(payload, _LOCATIONS):
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
    colliding_macs: frozenset[int],
    mac_by_device_id: Mapping[int, int],
) -> dict[int, DesignerRecord]:
    """Join each object to its module's description entry.

    The key is ``(DESC_TYPE_BY_KIND[typ_komponentu], leaf_io_no)`` within
    the module record of ``module_mac``. A leafless object joins through
    ``mac_by_device_id[id_urzadzenia]`` and ``funkcja - 1`` instead: its
    module row's mac, and the channel every leafed object of these kinds
    embeds as ``leaf_io_no`` (docs/identity.md). Objects on a colliding
    mac are skipped - the reply cannot be attributed to one module.
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
        if obj.leaf_id:
            mac = obj.module_mac
            out_no = obj.leaf_io_no
        else:
            mac = mac_by_device_id.get(obj.id_urzadzenia)
            out_no = obj.funkcja - 1
        if mac is None or out_no is None:
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


# The `(typ_urzadzenia, wersja_pcb)` pairs whose panel params layout is
# live-proven. The Designer keys the layout by the same pair, and other
# boards use a different one - an older revision puts the touch field
# colour at offset 1 as three bytes with no white channel, and shifts the
# masks. Reading one of those with this layout would produce confident
# wrong values, so an unlisted pair resolves nothing. Extend only with a
# pair read off real hardware.
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

    The section is laid out by the field count: the colours, then one
    light-signal byte per field, the beep time, and three field masks of
    ``ceil(fields / 8)`` bytes each. None when the blob is too short to
    hold the whole section - a truncated blob must not read as confident
    values. docs/description-records.md carries the offsets.
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
    colliding_macs: frozenset[int],
) -> dict[int, PanelSettings]:
    """The panel settings of every module whose layout is proven, by mac.

    A module resolves only when its ``(typ_urzadzenia, wersja_pcb)`` pair
    is a proven layout and it advertises a backlight channel count - that
    count is the number of touch fields. Everything else resolves
    nothing, so a module that is not a panel, a board this library has
    not read, and a colliding mac are all simply absent.
    """
    out: dict[int, PanelSettings] = {}
    for mac, blob in params_by_mac.items():
        if mac in colliding_macs:
            continue
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


def resolve_module_capabilities(
    capabilities_by_mac: Mapping[int, Mapping[int, int]],
    colliding_macs: frozenset[int],
) -> dict[int, Mapping[int, int]]:
    """The capability map of every answering module, by mac.

    An empty map is authoritative: the module answered and advertised
    nothing. Colliding macs are skipped, exactly as the record side skips
    them - the reply cannot be attributed.
    """
    return {
        mac: caps
        for mac, caps in capabilities_by_mac.items()
        if mac not in colliding_macs
    }


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


def parse_server_info(payload: str) -> AmpioServerInfo:
    """Parse a server-info payload, keeping only the safe fields.

    The baseline server wraps the fields in a ``Results`` object and always
    reports two things: its ``mac``, the identity every consumer scopes a
    registry by, and ``userId``, the asking account. A payload missing any
    of the three is refused, so every :class:`AmpioServerInfo` carries a
    populated :pyattr:`AmpioServerInfo.server_key` and a readable
    :pyattr:`AmpioServerInfo.access_tier`.
    """
    try:
        outer = json.loads(payload)
    except (ValueError, TypeError) as err:
        raise AmpioProtocolError(f"The Ampio {_INFO} reply is not JSON") from err
    data = outer.get("Results") if isinstance(outer, dict) else None
    if not isinstance(data, dict):
        raise AmpioProtocolError(f"The Ampio {_INFO} reply carries no `Results` object")
    return AmpioServerInfo(
        mac=_int_column(data, "mac", _INFO),
        user_id=_int_column(data, "userId", _INFO),
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


def parse_states_snapshot(payload: str) -> list[SnapshotEntry]:
    """Parse a bulk `data/states` snapshot.

    The reply lists the objects that hold a value, so every row carries
    its own `stan_json`.
    """
    return [
        SnapshotEntry(
            id=_int_column(row, "id", _SNAPSHOT),
            stan_json=_text_column(row, "stan_json", _SNAPSHOT),
        )
        for row in require_rows(payload, _SNAPSHOT)
    ]


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
    block: int | None = None
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
        block = to_int(data.get("block"))
        thermostat = _parse_thermostat(data)
    return StateUpdate(
        id=oid,
        state=state,
        on_ms=on_ms,
        lammel=lammel,
        block=block,
        thermostat=thermostat,
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
        block=to_int(data.get("block")),
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
    # runs it exactly once, and the parsed value is what a fetch returns. A
    # reply the parser refuses raises `AmpioProtocolError`, which neither
    # resolves a fetch nor latches discovery. None marks an endpoint whose
    # reply mutates state - its AmpioStore handler is the gate instead.
    parses: Callable[[str], object] | None = None
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
    Endpoint(
        "groups", "data", "groups", "data", "groups", parses=_rows_parser("room table")
    ),
    Endpoint(
        "group_devices",
        "data",
        "group_devices",
        "data",
        "group_devices",
        parses=_rows_parser("room membership table"),
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
    index - :pyattr:`AmpioObject.leaf_io_no`, one below the 1-based raw
    state channel.
    """
    return f"{function:02x}f9{value:02x}{channel:02x}"


# The Designer's "test condition" button executes one module action at
# once: the `0c07` envelope, a trigger state byte, then the action. State
# 3 asserts and 0 releases, and the button sends 3 on press. Every action
# the library sends asserts, so the prefix carries the 3.
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
# Every action below uses the vendor's own sub-function 1, the code the
# stored conditions on live modules carry. For the backlight, 1 and 2
# both set the resting colour and neither outranks the other.
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
    None selects every field: all bits set, which each panel reads down
    to the fields it actually has.
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
    """The per-field backlight colour action as ASCII hex."""
    return (
        f"{_ACTION_FRAME_PREFIX}{_BACKLIGHT_ACTION}{_ACTION_SUB_FUNCTION}"
        f"{red:02x}{green:02x}{blue:02x}{white:02x}{mask}"
    )


def raw_status_light_payload(red: int, green: int, blue: int, mask: str) -> str:
    """The per-field status indicator colour action as ASCII hex.

    The same shape as the backlight action, minus the white channel: the
    status indicator has no white.
    """
    return (
        f"{_ACTION_FRAME_PREFIX}{_STATUS_LIGHT_ACTION}{_ACTION_SUB_FUNCTION}"
        f"{red:02x}{green:02x}{blue:02x}{mask}"
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
# addressed to the module. 1 lights the module's CAN LED steadily (a M-DOT
# lights the LED on its back), 0 returns it to its blink. The module holds
# identify until the stop frame; the Designer's 30 s auto-stop is its own
# timer. No echo follows on any topic. docs/panel-writes.md ("Module
# identify") carries the wire facts.
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


# The app-sync tables whose retained `md5/<keyword>` digest the M-SERV
# rewrites when a Designer save changes them. The admin tier watches these
# to learn that its `config` catalogues went stale (docs/discovery-flow.md).
CATALOGUE_DIGEST_KEYWORDS = ("devices", "params_devices")


def md5_topic(user: str, keyword: str) -> str:
    """Retained topic holding the MD5 of an account's app-sync table reply."""
    return f"ampio/fromDB/{user}/md5/{keyword}"


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


# typ_komponentu -> description class (descType), live-proven pairs only
# (docs/description-records.md): an unlisted kind resolves no location.
# Extend only with a live-proven pair.
DESC_TYPE_BY_KIND: dict[str, int] = {
    "przekaznik": 12,  # OUTPUTS
    "roleta_procenty": 26,  # ROLLER
    "roleta_lamelki": 26,  # ROLLER
    "led": 16,  # OUT_OC_U8
    "rgbw": 34,  # RGBW output class; no symbolic name in the recovered enum
    "flaga": 6,  # FLAG_BIN
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
    | DiagnosticsReport
    | DeviceList
    | CatalogueDigest
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
        if len(parts) == 4 and parts[3] == "event":
            number = to_int(payload.strip())
            return (
                None if number is None else BusEventRaised(event_number=number, mac=mac)
            )
        return None
