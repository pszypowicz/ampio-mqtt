"""Data models for Ampio DB objects."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from .classification import (
    ObjectKind,
    OutputKind,
    classify,
    is_system_type,
)
from .device_types import Mounting, module_model, module_mounting


class AccessTier(Enum):
    """Account tier, decided by the authenticated login name.

    The M-SERV gates the ``config`` surface (and the raw ``ampio/from/#``
    channel tree) on the account being the reserved ``admin`` login; the
    per-user app permissions do not affect it. A non-admin account, however
    permissioned, is served only the app-sync ``data`` surface.
    """

    ADMIN = "admin"  # the reserved `admin` login: full catalogue + modules
    RESTRICTED = "restricted"  # an app-created user: app-sync view only


# Bit flags inside the `params` integer (`obiekty.params`); the names come
# from the Designer web bundle's own enum, and the semantics of the bits
# read here are corroborated by the M-SERV's Matter bridge and by live
# probing (docs/identity.md). Bit 4 is the hidden/stub marker (see
# `AmpioObject.hidden`); bit 6 is the Designer read-only checkbox (see
# `AmpioObject.read_only`); bit 37 is the per-object Matter opt-in, not a
# visibility signal, and nothing here reads it. Bit 15 is the generic
# `OPTION1` slot, whose meaning depends on the component type - Designer
# labels it "Bell object" on `przekaznik` and `flaga` only, so
# `AmpioObject.bell` gates on the type before reading it.
_HIDDEN_FLAG = 1 << 4
_READ_ONLY_FLAG = 1 << 6
_BELL_FLAG = 1 << 15
# The component types whose Designer editor renders `OPTION1` as the
# "Bell object" checkbox. On every other type the bit means something
# else (slider layout, lamella step, ...), so it must not read as bell.
_BELL_TYPES = frozenset({"przekaznik", "flaga"})
# The component types whose Designer editor renders the `czas` column as
# the "turn-on time" field - the Designer bundle's own list. A camera
# reads the same column as a refresh time in milliseconds, and no other
# type gets the field, so `AmpioObject.pulse_ms` gates on the type.
_PULSE_TYPES = frozenset(
    {
        "flaga",
        "flaga_l",
        "flaga_p",
        "przekaznik",
        "led",
        "flaga_liniowa",
        "flaga_liniowa16",
        "rgb",
        "rgbww",
        "ledww",
    }
)

# The `leafId` shape: `0_<macHex>_<sfId>_<subSfId>_<ioNo>` - a leading
# literal `0`, then the four fields the regex captures (docs/identity.md).
# Strict on purpose - a half-parsed mac that is wrong is worse than None.
_LEAF_ID_RE = re.compile(r"0_([0-9a-fA-F]+)_([^_]+)_([^_]+)_([^_]+)")


def leaf_mac(leaf_id: str) -> int | None:
    """The override mac a `leafId` embeds, or None for an empty or odd shape."""
    match = _LEAF_ID_RE.fullmatch(leaf_id)
    return int(match.group(1), 16) if match is not None else None


# The M-SERV's Designer override mac: its objects' leafId embeds this value
# (not the factory mac_global), and its own module row reports it as
# `AmpioModule.mac`. The one place the rule lives - consumers read
# `AmpioObject.is_server_owned` instead of comparing macs themselves, and
# the record sweep reads this name to skip the server's own row.
MSERV_MAC = 1


@dataclass(slots=True, frozen=True)
class ThermostatState:
    """A regulator's climate readback, from the rich `reg` state push.

    The push carries every field as a string; the library parses the
    temperatures and the cooling flag and passes the mode letter through
    verbatim, so a future unlisted letter loses nothing. The `A,S,M,H`
    vocabulary is in docs/protocol.md.
    """

    measure_temp: float | None
    set_temperature: float | None
    # Mode letter, verbatim from the wire.
    mode: str | None
    # The push's cooling flag; `"0"` reads False, anything else True.
    cooling: bool | None


@dataclass(slots=True, frozen=True)
class DesignerRecord:
    """One object's entry of its module's CAN description record.

    Admin-guarded: only :meth:`AmpioClient.resolve_records` fills it, and
    only the admin tier can run that sweep, so ``AmpioObject.record`` is
    None on the restricted tier and before a sweep covers the object. A
    None field inside means the entry carries no value: an unassigned
    location, an untagged type, an empty description. docs/identity.md
    holds the wire shape, docs/account-tiers.md the tier rule.
    """

    location: str | None = None
    matter_device_type: int | None = None
    desc: str | None = None


@dataclass(slots=True, frozen=True)
class RecordSweep:
    """What one :meth:`AmpioClient.resolve_records` pass covered.

    ``records`` is the join result. The two mac sets separate the case a
    bare record map cannot: a module in ``answered_macs`` whose object
    still reads ``record`` None carries no entry for that output, while a
    module in ``silent_macs`` is catalogued but missing from the device
    list reply and says nothing either way. The M-SERV's own row is a
    device like any other in both sets.
    """

    records: Mapping[int, DesignerRecord]
    answered_macs: frozenset[int]
    silent_macs: frozenset[int]


@dataclass(slots=True, frozen=True)
class ModuleRecord:
    """The DEVICE_NAME entry of a module's CAN description record.

    Admin-guarded, exactly as :class:`DesignerRecord` is. ``location`` is
    the module-level "Lokalizacja" (where the box is mounted, not where
    its loads are) and ``desc`` the CAN-resident module name; either can
    differ from the admin catalogue row.
    """

    location: str | None = None
    desc: str | None = None


@dataclass(slots=True, frozen=True)
class AmpioObject:
    """A logical Ampio object (DB object) and its latest state.

    Frozen: an instance is an immutable snapshot. The store publishes a new
    instance on every change, so the one carried by an event stays what the
    event announced, and consumer code cannot corrupt the library's state
    through the read surface.
    """

    # The per-object identity source, exposed as `object_key`. An object
    # delete is soft on the `config` catalogue, so the autoincrement never
    # renumbers. `id_urzadzenia` is the volatile one: it mirrors the module
    # row, which is reassigned when a module is replaced.
    # docs/identity.md is the home for the identity model.
    id: int
    id_urzadzenia: int | None = None  # physical module
    typ_komponentu: str | None = None
    opis_menu: str | None = None
    interpretacja: int | None = None
    # Physical channel index within the module (obiekty.funkcja);
    # replacement-stable but NOT unique - objects can share one. Routes raw
    # channel events to this object.
    funkcja: int | None = None
    # `leafId`, identical on both discovery surfaces. Empty for system
    # objects, and Designer clears it when an object's Matter box is
    # unchecked. The physical-output key (`leaf_key`) and the parse source
    # for `module_mac` - docs/identity.md.
    leaf_id: str = ""
    # The override mac that leafed objects on the same `id_urzadzenia`
    # embed, read out of the catalogue this tier holds - a leafless
    # object's module on both tiers, None without such a sibling in the
    # grant. `module_mac` stays the leaf-parsed fact - docs/identity.md.
    sibling_module_mac: int | None = None
    # `params` bitfield (Designer config flags; see `hidden`/`visible`).
    # Defaults to 0 so a payload without the column reads "nothing hidden".
    params: int = 0
    # Matter device type ID from the Designer "Description in device" tag
    # (`type` column; "256" = 0x0100 On/Off Light). None when untagged. A
    # pure catalogue fact, served identically to both tiers and never
    # mutated after the seed; the record's own (fresher, admin-only) tag
    # is `record.matter_device_type`, and which one wins is the
    # consumer's choice. docs/identity.md holds the vocabulary and the
    # storage path.
    matter_device_type: int | None = None
    # The `czas` column as served, in the wire unit of 10 ms ticks. Its
    # meaning follows the component type: Designer's "turn-on time" on the
    # types `pulse_ms` reads, a refresh time in milliseconds on a camera.
    # 0 when not configured. Served on both tiers: `devicesDetails` carries
    # the column, and `data/params_devices` supplies it unfiltered where the
    # app-sync catalogue omits it.
    czas: int = 0
    # The object's description-record entry, admin sweep only; None on
    # the restricted tier and before a sweep covers the object.
    record: DesignerRecord | None = None
    # What this object is. Derived - never passed: computed from
    # `typ_komponentu` and `interpretacja` on every construction,
    # `dataclasses.replace` included, so no instance can hold a kind that
    # disagrees with its inputs (#94).
    kind: ObjectKind = field(init=False)
    # The state payload's `state` key, verbatim.
    state: str | None = None
    # Epoch seconds of the report `state` came from: the M-SERV's own `on`
    # timestamp when the report carried one, the local receive time for the
    # undated raw tree. Lets a later bulk snapshot be compared against what
    # is held instead of applied or dropped blind. None until any report
    # arrives, or when an undated seed supplied the value.
    updated_at: float | None = None
    # Whether the raw path owns this object (its raw-channel form has been
    # observed): per-object echoes and snapshot rows are then skipped -
    # resync is the broker's retained raw table. Admin tier only; cleared
    # when the raw index stops covering the object. docs/raw-channel-bridge.md.
    raw_owned: bool = False
    # Slat angle percent. Only tilt-capable covers report it.
    lammel: int | None = None
    # Climate readback, from the rich state shape only `reg` objects push.
    # None until a reg-shaped report arrives; a later report that lacks the
    # shape keeps the last readback, like `lammel` does.
    thermostat: ThermostatState | None = None

    def __post_init__(self) -> None:
        # Derived fields are set once, here, and nowhere else.
        object.__setattr__(
            self, "kind", classify(self.typ_komponentu, self.interpretacja)
        )

    @property
    def supports_tilt(self) -> bool:
        """Whether this object has a slat axis."""
        return isinstance(self.kind, OutputKind) and self.kind.tilt

    @property
    def is_on(self) -> bool:
        """Boolean interpretation of `state`, meaningful for input objects.

        Off when `state` is None/empty/`"0"`, on otherwise - so both the raw
        channel form (`"1"`) and the per-object form (`"255"`) read as on.
        """
        return self.state not in (None, "", "0")

    @property
    def numeric_value(self) -> float | None:
        """Numeric interpretation of `state`, meaningful for sensor objects.

        None when `state` is missing, not parseable as a number, or not
        finite - `float()` alone accepts forms like `"nan"`, `"inf"` and the
        overflowing `"1e999"`, which for a sensor reading are glitches rather
        than measurements.
        """
        if self.state is None:
            return None
        try:
            parsed = float(self.state)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None

    @property
    def rgbw(self) -> tuple[int, int, int, int] | None:
        """The four color channels, decoded from the packed state value.

        Only a color output (`OutputKind.color`) reads non-None - a
        dimmer's 0-255 level must not masquerade as a color. The packed
        form is ``R | G<<8 | B<<16 | W<<24``. A negative value is the same word
        in signed 32-bit form - the M-SERV's own Matter bridge emits that
        shape - and decodes identically. None when the value is missing,
        not an integer, or outside 32 bits.
        """
        if not (isinstance(self.kind, OutputKind) and self.kind.color):
            return None
        if self.state is None:
            return None
        try:
            packed = int(self.state)
        except ValueError:
            return None
        if not -(1 << 31) <= packed <= 0xFFFFFFFF:
            return None
        if packed < 0:
            packed += 1 << 32
        return (
            packed & 0xFF,
            (packed >> 8) & 0xFF,
            (packed >> 16) & 0xFF,
            (packed >> 24) & 0xFF,
        )

    @property
    def position(self) -> int | None:
        """Cover travel percent (0 closed, 100 open) from the state value.

        Anything but a position-capable cover (`OutputKind.position`)
        reads None, as does a value outside 0-100. The slat axis is
        :pyattr:`lammel`, exactly as `setRollerPos` splits them.
        """
        if not (isinstance(self.kind, OutputKind) and self.kind.position):
            return None
        if self.state is None:
            return None
        try:
            pos = int(self.state)
        except ValueError:
            return None
        return pos if 0 <= pos <= 100 else None

    @property
    def is_system(self) -> bool:
        """Whether this is a system object (always present regardless of grouping).

        ``symulacja`` (presence-simulation) and ``detekcja`` (detection) live
        outside the room/group hierarchy by design; the M-SERV always exposes
        them.
        """
        return is_system_type(self.typ_komponentu)

    @property
    def hidden(self) -> bool:
        """Whether the M-SERV flags this object as hidden / a stub (``params`` bit 4).

        The authoritative "do not surface" marker, honored by the
        M-SERV's own Matter bridge; it catches the phantom rows that
        duplicate a real Designer channel. See docs/identity.md.
        """
        return bool(self.params & _HIDDEN_FLAG)

    @property
    def read_only(self) -> bool:
        """Whether Designer marks this object read-only (``params`` bit 6).

        The M-SERV enforces the marker itself, on every account tier: an
        ``/api`` write to a read-only object is dropped before it reaches
        the CAN bus, with no echo and no error. Reads are unaffected. The
        checkbox can change at any time, so a consumer keeps the object's
        platform and rejects writes while this is True, rather than
        re-registering the entity. See docs/identity.md.
        """
        return bool(self.params & _READ_ONLY_FLAG)

    @property
    def bell(self) -> bool:
        """Whether Designer marks this object as a bell (``params`` bit 15).

        The "Bell object" checkbox: the object is meant for a single
        press, and the Ampio app renders it as a press-only button
        instead of a toggle. The checkbox exists on ``przekaznik`` and
        ``flaga`` only; on other component types bit 15 carries an
        unrelated per-type option, so this reads False for them. The
        marker is display intent - whether the output auto-releases is
        the module's own configuration. See docs/identity.md.
        """
        return self.typ_komponentu in _BELL_TYPES and bool(self.params & _BELL_FLAG)

    @property
    def pulse_ms(self) -> int:
        """Designer's "turn-on time" in milliseconds, 0 where the field does not exist.

        The ``czas`` column in 10 ms ticks, read on the component types
        whose Designer editor offers the field (relays, flags, dimmers,
        the RGB kinds). Every other type reads 0, a camera included, where
        the same column is a refresh time. The app reads the value as the
        default pulse length for a press; the M-SERV never applies it
        server-side, so a caller honors it by passing it to
        :meth:`AmpioClient.set_value` as ``pulse_ms``. See docs/identity.md.
        """
        if self.typ_komponentu not in _PULSE_TYPES:
            return 0
        return self.czas * 10

    @property
    def leaf_key(self) -> str | None:
        """The physical output this object drives (``leaf_<leaf_id>``), or None.

        Identical on both access tiers. It is not an identity for the
        object row. Several Designer views of one output share one
        ``leafId``, so two objects can return the same key. The
        per-object identity is :pyattr:`object_key`. None for an empty
        ``leaf_id`` (system objects, Matter box unchecked). See
        docs/identity.md.
        """
        return f"leaf_{self.leaf_id}" if self.leaf_id else None

    @property
    def object_key(self) -> str:
        """Snapshot-unique identity token (``obj_<id>``).

        The recommended per-object unique id: unique among every object
        in one discovery snapshot, served on both account tiers, and
        never None. Scope it per M-SERV with
        ``AmpioServerInfo.server_key``. The physical output an object
        drives is :pyattr:`leaf_key`, which several Designer views of one
        output share by design. See docs/identity.md.
        """
        return f"obj_{self.id}"

    @property
    def module_mac(self) -> int | None:
        """The owning module's effective bus mac, parsed from ``leaf_id``.

        The replacement-stable ``AmpioModule.mac``, served identically on
        both account tiers - the module key a consumer can group entities
        by even on a restricted account, which never receives the module
        catalogue (docs/identity.md). None when ``leaf_id`` is empty or,
        on no observed install, has an unexpected shape.
        """
        return leaf_mac(self.leaf_id)

    def _leaf_segment(self, group: int) -> int | None:
        """One numeric `leaf_id` segment, or None when it does not parse."""
        match = _LEAF_ID_RE.fullmatch(self.leaf_id)
        if match is None:
            return None
        try:
            return int(match.group(group))
        except ValueError:
            return None

    @property
    def sf_id(self) -> int | None:
        """The special-function id, the third ``leaf_id`` segment.

        The Designer's own name for the per-leaf function class. None when
        ``leaf_id`` is empty, malformed, or the segment is not a number.
        See docs/identity.md.
        """
        return self._leaf_segment(2)

    @property
    def sub_sf_id(self) -> int | None:
        """The sub-function id, the fourth ``leaf_id`` segment.

        Its meaning is scoped to :pyattr:`sf_id`. None when ``leaf_id`` is
        empty, malformed, or the segment is not a number. See
        docs/identity.md.
        """
        return self._leaf_segment(3)

    @property
    def leaf_io_no(self) -> int | None:
        """The I/O index within the module's description record.

        The last ``leaf_id`` segment, and the join key that pairs this
        object with its :class:`OutputDescription` entry. It covers inputs
        as well as outputs. None when ``leaf_id`` is empty, malformed, or
        the segment is not a number.
        """
        return self._leaf_segment(4)

    @property
    def is_server_owned(self) -> bool:
        """Whether this object belongs to the M-SERV itself.

        True when ``leaf_id`` embeds the M-SERV's override mac; works on
        both account tiers, so a consumer can anchor server-owned objects
        to its hub device without the module catalogue. False when
        ``leaf_id`` is empty.
        """
        return self.module_mac == MSERV_MAC

    @property
    def visible(self) -> bool:
        """Whether the M-SERV means to surface this object: ``not hidden``.

        The ``params`` DELETED bit is the one wire-side marker. ``leaf_id``
        says nothing here: Designer clears it when an object's Matter box
        is unchecked, and the row stays a real object. See docs/identity.md.
        """
        return not self.hidden


@dataclass(slots=True, frozen=True)
class AmpioModule:
    """A physical Ampio module (urzadzenie) that owns objects.

    Frozen, exactly as :class:`AmpioObject` is.
    """

    id: int
    # The effective bus address (Designer "MAC override"), keying the raw
    # `ampio/from/<MAC>/...` topics. Replacement-stable - prefer it over
    # `mac_global` as the module key; may be a non-unique default (the
    # M-SERV is 1). docs/identity.md carries the full identity model.
    mac: int | None = None  # devices.mac (override / effective bus address)
    # Factory-burned hardware id; CHANGES when the unit is replaced.
    mac_global: int | None = None  # devices.mac_global (factory id)
    nazwa_urzadzenia: str | None = None  # user-given module name
    typ_urzadzenia: int | None = None
    # Resolved model name for `typ_urzadzenia`. Derived - never passed:
    # computed on every construction (#94), None when `typ_urzadzenia` is
    # unknown or missing.
    model: str | None = field(init=False)
    # Curated mounting class for `typ_urzadzenia` ("cabinet" DIN rail /
    # "wall" / "flush" in-box), derived exactly like `model` (#115).
    # Decoration for device info only - never a topology input. None when
    # unclassified.
    mounting: Mounting | None = field(init=False)
    wersja_softu: int | None = None
    wersja_pcb: int | None = None
    # The module's DEVICE_NAME record entry, admin sweep only; None
    # until a sweep covers the module.
    record: ModuleRecord | None = None
    # Local epoch seconds when this process last received live evidence of
    # the module: a state push or raw edge for one of its objects, or its own
    # diagnostics broadcast. One clock only - snapshot and catalogue seeds do
    # not count, since they replay DB state that may be arbitrarily old. None
    # until the first live message after connect().
    last_seen: float | None = None
    # Self-reported health from the module's `b/4F` broadcast. Both stay None
    # on a standard account, which is not served the raw tree, and
    # `temperature` stays None on modules without the sensor.
    supply_voltage: float | None = None  # volts on the CAN bus
    temperature: float | None = None  # °C

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", module_model(self.typ_urzadzenia))
        object.__setattr__(self, "mounting", module_mounting(self.typ_urzadzenia))


@dataclass(slots=True, frozen=True)
class AmpioScene:
    """A named multi-action preset defined in the Ampio app."""

    id: int
    scene_name: str
    # The M-SERV's own enabled flag for the scene.
    active: bool = True
    # Parent scene when the install nests them; None for a top-level scene.
    parent_id: int | None = None
    # Objects the scene's actions touch, for a consumer that wants to relate a
    # scene to its entities.
    object_ids: frozenset[int] = field(default_factory=frozenset)


@dataclass(slots=True, frozen=True)
class AmpioServerInfo:
    """A safe subset of the Ampio M-SERV self-reported info.

    Intentionally excludes fields that would leak private data
    (geolocation, cloud endpoint, public key, user permissions).
    """

    mac: int  # the M-SERV's own CAN mac (matches a module's mac_global)
    # The asking account's id: -1 for the reserved `admin` login, the
    # users-table row id for an app-created user. See `access_tier`.
    user_id: int | None = None
    server_version: str | None = None  # the M-SERV server application's version
    server_revision: str | None = None
    mqtt_version: str | None = None  # broker version
    local_ip: str | None = None  # used for the configuration_url
    device_id: str | None = None  # hardware identifier of the host

    @property
    def server_key(self) -> str:
        """Canonical scoping key for this M-SERV, for consumer registries.

        The string to prefix per-server artifacts with - unique ids, device
        identifiers: the decimal form of ``mac``, which is what every known
        consumer already derived by hand. The format is a stable promise;
        never parse or reformat it. An info reply without a ``mac`` does not
        parse, so every :class:`AmpioServerInfo` a consumer can hold has one.
        """
        return str(self.mac)

    @property
    def access_tier(self) -> AccessTier | None:
        """Account tier per the account id in the info reply, or None.

        The wire's own confirmation for a config flow reading a
        :meth:`AmpioClient.check_connection` result; a running client's
        operational tier comes from the authenticated username instead.
        The reserved ``admin`` login reports the pseudo-user id ``-1``;
        app-created users carry a positive row id and are always the
        standard tier (docs/account-tiers.md). None when the reply carried
        no ``userId``.
        """
        if self.user_id is None:
            return None
        return AccessTier.ADMIN if self.user_id == -1 else AccessTier.RESTRICTED


@dataclass(slots=True)
class ConnectionStats:
    """Internal liveness counters behind ``diagnostics_snapshot()``.

    Updated by the connection layer (`last_message_at` by the client).
    `started_at` and `reconnect_count` cover the current ``connect()`` run -
    a deliberate disconnect/connect restarts them, so a snapshot never reads a
    consumer-initiated restart as a flapping connection. `last_error` and
    `last_message_at` roll across runs.
    """

    reconnect_count: int = 0  # reconnects within the current run
    last_error: str | None = None  # overwritten on every failed attempt
    started_at: float | None = None  # epoch seconds of the run's first connect
    last_message_at: float | None = None  # epoch seconds of last MQTT message in
    # Subscriptions the broker rejected in the SUBACK of the latest
    # (re)connect: topic -> reason code. Replaced wholesale on every connect,
    # so an empty dict means the current session got everything it asked for.
    # The subscribe set is tier-shaped, so every filter should be granted;
    # a rejection is warned but does not fail the connection.
    subscribe_failures: dict[str, int] = field(default_factory=dict)
