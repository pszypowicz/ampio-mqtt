"""Pure parsers for the Ampio DB-object MQTT protocol.

These helpers turn raw MQTT payloads into typed structures with no I/O and no
state mutation - the `AmpioClient` is the only thing that applies them to
`AmpioState`. Keeping the parsing isolated makes it trivially unit-testable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .device_types import module_capabilities, module_model
from .models import AmpioModule, AmpioServerInfo

if TYPE_CHECKING:
    import aiomqtt


# CONNACK return codes / reason strings that indicate an auth failure rather
# than a network or transport problem. aiomqtt surfaces the broker text in the
# `MqttError` message; matching is heuristic but covers MQTT 3.1.1 (rc 4/5) and
# MQTT 5 (`not authorized`, `bad user name or password`). The match runs over
# `MqttError.__str__`, so revisit this table if aiomqtt changes its error
# formatting (or when bumping to aiomqtt v3).
_AUTH_ERROR_MARKERS = (
    "not authorized",
    "bad user name",
    "bad username",
    "unauthorized",
    "rc=4",
    "rc=5",
    "[code:4]",
    "[code:5]",
    "[code:134]",
    "[code:135]",
)


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
    group_ids: frozenset[int]  # parsed `powiazane` GROUP_CONCAT (rare in practice)
    params: int  # `params` bitfield; bit 4 = hidden/stub, bit 37 = matter-exposed
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


@dataclass(slots=True)
class StanJsonSeed:
    """Initial `state` value and server timestamp extracted from `stan_json`."""

    value: str | None
    on_ms: int | float | None


def is_auth_error(err: aiomqtt.MqttError) -> bool:
    """Return True if the MQTT error looks like an authentication failure."""
    msg = str(err).lower()
    return any(marker in msg for marker in _AUTH_ERROR_MARKERS)


def to_int(value: Any) -> int | None:
    """Best-effort int coercion that returns None on bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_details(payload: str) -> list[ObjectMetadata] | None:
    """Parse a `devicesDetails` payload into per-object metadata.

    Returns None when the payload is not parseable JSON; an empty list is a
    valid (empty) response.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    out: list[ObjectMetadata] = []
    for item in data.get("List", []):
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
                group_ids=_parse_powiazane(item.get("powiazane")),
                # Absent / non-numeric -> 0 (no flags); `params` can exceed 32
                # bits (the matter-exposed flag is bit 37), which Python ints
                # handle natively.
                params=to_int(item.get("params")) or 0,
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


def _parse_powiazane(value: Any) -> frozenset[int]:
    """Parse the GROUP_CONCAT `powiazane` field into a set of group ids.

    Most M-SERV firmware does not emit this column in `devicesDetails`; the
    parser keeps it for any future build that does, but real installs see an
    empty result. Group membership is enriched from the separate
    ``data/groups`` / ``data/group_devices`` join via ``fetch_rooms()``.
    """
    if not isinstance(value, str) or not value or value == "NULL":
        return frozenset()
    ids: set[int] = set()
    for piece in value.split(","):
        gid = to_int(piece.strip())
        if gid is not None:
            ids.add(gid)
    return frozenset(ids)


def parse_devices(payload: str) -> list[AmpioModule] | None:
    """Parse a `devices` payload into a list of physical modules.

    Returned modules have `last_seen=None`; the caller preserves any existing
    `last_seen` from a prior discovery.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    out: list[AmpioModule] = []
    for item in data.get("List", []):
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


def parse_server_info(payload: str) -> AmpioServerInfo:
    """Parse a server-info payload, keeping only the safe fields.

    Returns an empty `AmpioServerInfo` on parse failure - callers treat that as
    "info not available yet" rather than an error.
    """
    try:
        outer = json.loads(payload)
    except (ValueError, TypeError):
        return AmpioServerInfo()
    data = outer.get("Results", outer) if isinstance(outer, dict) else {}
    if not isinstance(data, dict):
        return AmpioServerInfo()
    return AmpioServerInfo(
        mac=to_int(data.get("mac")),
        server_version=data.get("serverVersion") or None,
        server_revision=data.get("serverRevision") or None,
        mqtt_version=data.get("mqttVersion") or None,
        local_ip=data.get("local_ip") or None,
        device_id=data.get("device_id") or None,
    )


def parse_states_snapshot(payload: str) -> list[SnapshotEntry] | None:
    """Parse a bulk `data/states` snapshot."""
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    out: list[SnapshotEntry] = []
    for item in data.get("List", []):
        oid = to_int(item.get("id"))
        if oid is None:
            continue
        out.append(SnapshotEntry(id=oid, stan_json=item.get("stan_json") or None))
    return out


def parse_state_message(topic: str, payload: str) -> StateUpdate | None:
    """Parse a live `ampio/fromDB/<user>/ob/<id>/state` push.

    Returns None when the topic does not match the expected shape; otherwise a
    `StateUpdate`. The payload may be plain text or a JSON object with a
    `state` field; in either case `value` is set, and `on_ms` is populated
    when the payload carried a server timestamp.
    """
    parts = topic.split("/")
    if len(parts) < 6 or parts[3] != "ob":
        return None
    oid = to_int(parts[4])
    if oid is None:
        return None
    value: str = payload
    on_ms: int | float | None = None
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
    return StateUpdate(id=oid, value=value, on_ms=on_ms)


def parse_raw_channel_topic(topic: str) -> tuple[int, str, int] | None:
    """Parse a raw `ampio/from/<MAC>/state/<prefix>/<channel>` topic.

    Returns `(mac, prefix, channel)` where `mac` is the hex MAC segment parsed
    as an int (so leading-zero / upper-vs-lower differences never matter) and
    `channel` is the decimal channel index. Returns None on any shape mismatch.
    The MAC is the module's effective CAN bus address (the Designer override),
    matching `AmpioModule.mac`.
    """
    parts = topic.split("/")
    if len(parts) != 6 or parts[0] != "ampio" or parts[1] != "from":
        return None
    if parts[3] != "state":
        return None
    try:
        mac = int(parts[2], 16)
    except ValueError:
        return None
    channel = to_int(parts[5])
    if channel is None:
        return None
    return mac, parts[4], channel


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
    )
