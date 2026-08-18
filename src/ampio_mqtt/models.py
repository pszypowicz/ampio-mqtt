"""Data models for Ampio DB objects."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from .classification import (
    InputKind,
    ObjectKind,
    OutputKind,
    SensorKind,
    ThermostatKind,
    is_system_type,
)
from .device_types import Capability
from .endpoints import AccessTier

# Bit flags inside the `params` integer (`obiekty.params`). The semantics
# match the M-SERV's own Matter bridge, which selects exposable objects with
# `(params & 2**37) and not (params & 16)`. See docs/matter-bridge.md.
#
# - bit 4 (`16`): the object is hidden / a stub. The M-SERV sets it on the
#   phantom rows that duplicate a real Designer channel (same leafId, no value)
#   and on objects the user removed/hid. It is the authoritative "do not
#   surface this" marker, and unlike the DB `id` it is replacement-stable.
# Bit 37 marks a per-object Matter opt-in; it is not a visibility signal and
# nothing here reads it. See docs/matter-bridge.md.
_HIDDEN_FLAG = 1 << 4


@dataclass(slots=True)
class AmpioObject:
    """A logical Ampio object (DB object) and its latest state."""

    # Volatile: `id` (and `device_id`, the owning id_urzadzenia) are DB
    # autoincrement ids assigned in hardware (`mac_global`) order. They change
    # when a module is replaced - do NOT use them as durable identity across a
    # hardware swap. See `funkcja` and `AmpioModule.mac` for replacement-stable
    # values.
    id: int
    device_id: int | None = None  # id_urzadzenia (physical module); see note above
    typ_komponentu: str | None = None
    name: str | None = None
    interpretacja: int | None = None
    # Physical channel index within the module (obiekty.funkcja). Survives module
    # replacement (it is part of the re-loaded Designer config), but is NOT a
    # unique object id - multiple objects, even active ones, can share a funkcja
    # (the same physical signal exposed as several Designer objects). Used to
    # route raw channel events to this object.
    funkcja: int | None = None
    # `leafId` from `devicesDetails` / the app-sync `data/devices` catalogue -
    # a short token like ``0_cb8f_76_0_0``, identical on both surfaces. The
    # wire payload sets it for every "real" object; ghost rows that survived a
    # Designer removal AND system objects (Simulation / Detection) both come
    # back with an empty string. Doubles as the visibility marker (see
    # `visible`) and the stable identity source (see `stable_key`).
    leaf_id: str = ""
    # `params` bitfield from `devicesDetails` (admin tier) or the
    # `data/params_devices` table (every tier). Replacement-stable Designer
    # config flags; see `_HIDDEN_FLAG`, `hidden`, and `visible`.
    # Defaults to 0 so a payload without the column reads as
    # "nothing hidden" and the visibility rule falls back to the leaf_id
    # heuristic alone.
    params: int = 0
    # What this object is, once metadata has arrived. Exactly one kind applies.
    kind: ObjectKind | None = None
    value: str | None = None
    # Epoch seconds of the report `value` came from, in whichever domain
    # `updated_at_clock` names. Lets a later bulk snapshot be compared
    # against what is already held instead of being applied or dropped blind.
    updated_at: float | None = None
    # Which clock stamped `updated_at`: "server" for the M-SERV's own `on`
    # field, "local" for the receive clock of the undated raw tree (whose
    # per-object echo, due ~150 ms after an edge, re-anchors the object to
    # the server clock). Timestamps are only comparable within one domain -
    # an unsynced M-SERV's RTC can be arbitrarily wrong - and the library
    # never compares across them; neither should a consumer. None until any
    # report arrives, or when an undated seed supplied the value.
    updated_at_clock: Literal["server", "local"] | None = None
    # Whether this input's raw-channel form has been observed. From then on
    # the raw path is authoritative: the slower per-object echo no longer
    # re-notifies or overwrites, and only anchors `updated_at` to the server
    # clock. Only ever True on the admin tier, which alone receives the raw
    # tree; cleared when the raw index stops covering the object.
    raw_proven: bool = False
    # Slat angle percent, from the `lammel` state field. Only tilt-capable
    # covers report it.
    tilt_position: int | None = None

    @property
    def is_sensor(self) -> bool:
        """Whether this object is exposed by the sensor platform."""
        return isinstance(self.kind, SensorKind)

    @property
    def is_input(self) -> bool:
        """Whether this object is exposed by the binary_sensor/input platform."""
        return isinstance(self.kind, InputKind)

    @property
    def is_output(self) -> bool:
        """Whether this object accepts commands (switch/light/cover platforms)."""
        return isinstance(self.kind, OutputKind)

    @property
    def is_thermostat(self) -> bool:
        """Whether this is a temperature controller (climate platform).

        Its `value` is the running flag (`is_on` applies); the setpoint is
        driven with :meth:`AmpioClient.set_temperature`, and the rich
        readback is tracked in #73.
        """
        return isinstance(self.kind, ThermostatKind)

    @property
    def supports_tilt(self) -> bool:
        """Whether this object has a slat axis."""
        return isinstance(self.kind, OutputKind) and self.kind.tilt

    @property
    def is_on(self) -> bool:
        """Boolean interpretation of `value`, meaningful for input objects.

        Off when `value` is None/empty/`"0"`, on otherwise - so both the raw
        channel form (`"1"`) and the per-object form (`"255"`) read as on.
        """
        return self.value not in (None, "", "0")

    @property
    def numeric_value(self) -> float | None:
        """Numeric interpretation of `value`, meaningful for sensor objects.

        None when `value` is missing, not parseable as a number, or not
        finite - `float()` alone accepts forms like `"nan"`, `"inf"` and the
        overflowing `"1e999"`, which for a sensor reading are glitches rather
        than measurements.
        """
        if self.value is None:
            return None
        try:
            parsed = float(self.value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None

    @property
    def is_system(self) -> bool:
        """Whether this is a system object (always present regardless of grouping).

        ``symulacja`` (presence-simulation) and ``detekcja`` (detection) live
        outside the room/group hierarchy by design; the M-SERV always exposes
        them. Used by :pyattr:`visible` so consumers do not have to hardcode
        the membership rule.
        """
        return is_system_type(self.typ_komponentu)

    @property
    def hidden(self) -> bool:
        """Whether the M-SERV flags this object as hidden / a stub (``params`` bit 4).

        This is the authoritative "do not surface" marker - the same one the
        M-SERV's own Matter bridge honours. It catches the phantom rows that
        duplicate a real Designer channel (sharing its ``leaf_id`` but carrying
        no value), which the ``leaf_id`` heuristic alone lets through. See
        docs/matter-bridge.md.
        """
        return bool(self.params & _HIDDEN_FLAG)

    @property
    def stable_key(self) -> str | None:
        """Replacement-stable identity token (``leaf_<leaf_id>``), or None.

        The recommended per-object unique id (see docs/identity.md).
        ``leaf_id`` is part of the reloaded Designer config, so it survives a
        module swap; the ``config`` and ``data`` discovery surfaces report the
        same value, so the key is identical on both access tiers; and it is
        unique among ``visible`` objects - hidden phantom stubs share their
        twin's ``leaf_id``, so filter on ``visible`` first. Objects with an
        empty ``leaf_id`` (system objects, ghost rows) return None; a consumer
        surfacing those needs its own fallback key. Scope the key per M-SERV
        by prefixing with a server identifier (e.g. ``AmpioServerInfo.mac``).
        """
        return f"leaf_{self.leaf_id}" if self.leaf_id else None

    @property
    def visible(self) -> bool:
        """Whether the object is one the user can see in Designer's tree.

        ``hidden`` (``params`` bit 4) takes precedence: a hidden object is never
        visible, even with a populated ``leaf_id`` - this is what drops the
        phantom half of a duplicated Designer channel. Otherwise the wire-side
        marker is ``leaf_id``, set for every real object and empty for ghost
        rows and system objects, with the latter pulled back in by
        ``is_system``. When ``params`` is absent (so ``hidden`` is False) the
        ``leaf_id`` test alone decides.
        """
        if self.hidden:
            return False
        return bool(self.leaf_id) or self.is_system


@dataclass(slots=True)
class AmpioModule:
    """A physical Ampio module (urzadzenie) that owns objects."""

    id: int
    # Designer-assignable CAN bus address (the "MAC override"). This is the
    # effective address the module uses on the bus and the one the raw
    # `ampio/from/<MAC>/...` topics are keyed by. It is replacement-stable: a
    # replacement unit is re-stamped with the dead one's value so other devices'
    # CAN logic needs no reprogramming. May be a non-unique default (the M-SERV
    # is 1). Prefer this over `mac_global` for a replacement-stable module key.
    mac: int | None = None  # devices.mac (override / effective bus address)
    # Factory-burned, globally-unique hardware id. CHANGES when the physical unit
    # is replaced, so it is not stable identity across a hardware swap.
    mac_global: int | None = None  # devices.mac_global (factory id)
    name: str | None = None  # nazwa_urzadzenia (user-given module name)
    type: int | None = None  # typ_urzadzenia
    model: str | None = None  # resolved model name for `type`
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    sw_version: int | None = None  # wersja_softu
    hw_version: int | None = None  # wersja_pcb
    # Local epoch seconds when this process last received live evidence of
    # the module: a state push or raw edge for one of its objects, or its own
    # diagnostics broadcast. One clock only - snapshot and catalogue seeds do
    # not count, since they replay DB state that may be arbitrarily old. None
    # until the first live message after start().
    last_seen: float | None = None
    # Self-reported health from the module's `b/4F` broadcast. Both stay None
    # on a standard account, which is not served the raw tree, and
    # `temperature` stays None on modules without the sensor.
    supply_voltage: float | None = None  # volts on the CAN bus
    temperature: float | None = None  # °C


@dataclass(slots=True)
class AmpioScene:
    """A named multi-action preset defined in the Ampio app."""

    id: int
    name: str
    # The M-SERV's own enabled flag for the scene.
    active: bool = True
    # Parent scene when the install nests them; None for a top-level scene.
    parent_id: int | None = None
    # Objects the scene's actions touch, for a consumer that wants to relate a
    # scene to its entities.
    object_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass(slots=True)
class AmpioServerInfo:
    """A safe subset of the Ampio M-SERV self-reported info.

    Intentionally excludes fields that would leak private data
    (geolocation, cloud endpoint, public key, user permissions).
    """

    mac: int | None = None  # the M-SERV's own CAN mac (matches a module's mac_global)
    # The asking account's id: -1 for the reserved `admin` login, the
    # users-table row id for an app-created user. See `access_tier`.
    user_id: int | None = None
    server_version: str | None = None  # ampio_mqtt application version
    server_revision: str | None = None
    mqtt_version: str | None = None  # broker version
    local_ip: str | None = None  # used for the configuration_url
    device_id: str | None = None  # hardware identifier of the host

    @property
    def access_tier(self) -> AccessTier:
        """Account tier, derived from the account id in the info reply.

        The M-SERV's administrator is the reserved ``admin`` login, reported
        as the pseudo-user id ``-1``; app-created users carry their positive
        users-table row id and are always the standard tier - the app offers
        no administrator toggle for them, and their per-object permissions
        never open the admin-only surfaces. ``UNKNOWN`` when the reply
        carried no ``userId``, which no baseline server produces (see the
        supported-versions policy in the README).
        """
        if self.user_id is None:
            return AccessTier.UNKNOWN
        return AccessTier.ADMIN if self.user_id == -1 else AccessTier.RESTRICTED


@dataclass(slots=True)
class ConnectionStats:
    """Lightweight liveness counters surfaced for downstream diagnostics.

    Updated by `AmpioClient` itself; values are monotonic except `last_error`
    (overwritten on every reconnect attempt). Intended for HA's per-config-
    entry diagnostics blob so a maintainer can correlate a "flapping" report
    with the actual reconnect count seen by the client.
    """

    reconnect_count: int = 0
    last_error: str | None = None
    started_at: float | None = None  # epoch seconds of first successful connect
    last_message_at: float | None = None  # epoch seconds of last MQTT message in
    # Subscriptions the broker rejected in the SUBACK of the latest
    # (re)connect: topic -> reason code. Replaced wholesale on every connect,
    # so an empty dict means the current session got everything it asked for.
    # A rejection does not fail the connection - the admin-only raw tree is
    # expected to be denied to a standard account on brokers that enforce it.
    subscribe_failures: dict[str, int] = field(default_factory=dict)
