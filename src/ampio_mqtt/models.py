"""Data models for Ampio DB objects."""

from __future__ import annotations

import math
import re
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
# visibility signal, and nothing here reads it.
_HIDDEN_FLAG = 1 << 4
_READ_ONLY_FLAG = 1 << 6

# The `leafId` shape: `0_<macHex>_<F2>_<F3>_<F4>`, the same structure the
# M-SERV's own Matter bridge parses (docs/identity.md). Only the mac
# segment is extracted; the F segments' meaning stays opaque. Strict on
# purpose - a half-parsed mac that is wrong is worse than None.
_LEAF_ID_RE = re.compile(r"0_([0-9a-fA-F]+)_[^_]+_[^_]+_([^_]+)")

# The M-SERV's Designer override mac: its objects' leafId embeds this value
# (not the factory mac_global), and its own module row reports it as
# `AmpioModule.mac`. The one place the rule lives - consumers read
# `AmpioObject.is_server_owned` instead of comparing macs themselves.
_MSERV_MAC = 1


@dataclass(slots=True, frozen=True)
class ThermostatState:
    """A regulator's climate readback, from the rich `reg` state push.

    The push carries every field as a string; the library parses the
    temperatures and the cooling flag and passes the mode letter through
    verbatim, so a future unlisted letter loses nothing. The `A,S,M,H`
    vocabulary is in docs/protocol.md.
    """

    measured_temperature: float | None
    target_temperature: float | None
    # Mode letter, verbatim from the wire.
    mode: str | None
    # The push's cooling flag; `"0"` reads False, anything else True.
    cooling: bool | None


@dataclass(slots=True, frozen=True)
class AmpioObject:
    """A logical Ampio object (DB object) and its latest state.

    Frozen: an instance is an immutable snapshot. The store publishes a new
    instance on every change, so the one carried by an event stays what the
    event announced, and consumer code cannot corrupt the library's state
    through the read surface.
    """

    # Volatile: `id` (and `device_id`) are DB autoincrement ids that change
    # when a module is replaced - never durable identity across a hardware
    # swap. docs/identity.md is the home for the identity model.
    id: int
    device_id: int | None = None  # id_urzadzenia (physical module)
    typ_komponentu: str | None = None
    name: str | None = None
    interpretacja: int | None = None
    # Physical channel index within the module (obiekty.funkcja);
    # replacement-stable but NOT unique - objects can share one. Routes raw
    # channel events to this object.
    funkcja: int | None = None
    # `leafId`, identical on both discovery surfaces; empty for ghost rows
    # and system objects. Doubles as the visibility marker (`visible`) and
    # the stable identity source (`stable_key`) - docs/identity.md.
    leaf_id: str = ""
    # `params` bitfield (Designer config flags; see `hidden`/`visible`).
    # Defaults to 0 so a payload without the column reads "nothing hidden".
    params: int = 0
    # Matter device type ID from the Designer "Description in device" tag
    # (`type` column; "256" = 0x0100 On/Off Light). None when untagged. When
    # set it is installer intent - the signal that a relay drives a light
    # rather than a plug - and consumers map it to a platform themselves;
    # `kind` stays derived from `typ_komponentu` alone. docs/identity.md
    # holds the vocabulary and the storage path.
    matter_device_type: int | None = None
    # Designer per-output location name (the "Lokalizacja" dropdown),
    # resolved from the module's CAN-resident description record by
    # `AmpioClient.resolve_locations()` - admin tier only. None until a
    # resolve ran, and for objects it could not match. docs/identity.md.
    location: str | None = None
    # What this object is. Derived - never passed: computed from
    # `typ_komponentu` and `interpretacja` on every construction,
    # `dataclasses.replace` included, so no instance can hold a kind that
    # disagrees with its inputs (#94).
    kind: ObjectKind = field(init=False)
    value: str | None = None
    # Epoch seconds of the report `value` came from: the M-SERV's own `on`
    # timestamp when the report carried one, the local receive time for the
    # undated raw tree. Lets a later bulk snapshot be compared against what
    # is held instead of applied or dropped blind. None until any report
    # arrives, or when an undated seed supplied the value.
    updated_at: float | None = None
    # Whether the raw path owns this object (its raw-channel form has been
    # observed): per-object echoes and snapshot rows are then skipped -
    # resync is the broker's retained raw table. Admin tier only; cleared
    # when the raw index stops covering the object. docs/raw-channel-bridge.md.
    raw_proven: bool = False
    # Slat angle percent, from the `lammel` state field. Only tilt-capable
    # covers report it.
    tilt_position: int | None = None
    # Climate readback, from the rich state shape only `reg` objects push.
    # None until a reg-shaped report arrives; a later report that lacks the
    # shape keeps the last readback, like `tilt_position` does.
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
        if self.value is None:
            return None
        try:
            packed = int(self.value)
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
        :pyattr:`tilt_position`, exactly as `setRollerPos` splits them.
        """
        if not (isinstance(self.kind, OutputKind) and self.kind.position):
            return None
        if self.value is None:
            return None
        try:
            pos = int(self.value)
        except ValueError:
            return None
        return pos if 0 <= pos <= 100 else None

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
    def stable_key(self) -> str | None:
        """Replacement-stable identity token (``leaf_<leaf_id>``), or None.

        The recommended per-object unique id, identical on both access
        tiers and unique among ``visible`` objects - filter on ``visible``
        first, and scope per M-SERV with ``AmpioServerInfo.key``. None for
        an empty ``leaf_id`` (system objects, ghost rows): a consumer
        surfacing those needs its own fallback key. See docs/identity.md.
        """
        return f"leaf_{self.leaf_id}" if self.leaf_id else None

    @property
    def module_mac(self) -> int | None:
        """The owning module's effective bus mac, parsed from ``leaf_id``.

        The replacement-stable ``AmpioModule.mac``, served identically on
        both account tiers - the module key a consumer can group entities
        by even on a restricted account, which never receives the module
        catalogue (docs/identity.md). None when ``leaf_id`` is empty or,
        on no observed install, has an unexpected shape.
        """
        match = _LEAF_ID_RE.fullmatch(self.leaf_id)
        return int(match.group(1), 16) if match is not None else None

    @property
    def leaf_out_no(self) -> int | None:
        """The output index within the module's description record.

        Parsed from ``leaf_id``'s last segment - the join key that pairs
        this object with its :class:`OutputDescription` entry
        (docs/identity.md). None when ``leaf_id`` is empty, malformed,
        or the segment is not a number.
        """
        match = _LEAF_ID_RE.fullmatch(self.leaf_id)
        if match is None:
            return None
        try:
            return int(match.group(2))
        except ValueError:
            return None

    @property
    def is_server_owned(self) -> bool:
        """Whether this object belongs to the M-SERV itself.

        True when ``leaf_id`` embeds the M-SERV's override mac; works on
        both account tiers, so a consumer can anchor server-owned objects
        to its hub device without the module catalogue. False when
        ``leaf_id`` is empty.
        """
        return self.module_mac == _MSERV_MAC

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
    name: str | None = None  # nazwa_urzadzenia (user-given module name)
    type: int | None = None  # typ_urzadzenia
    # Resolved model name for `type`. Derived - never passed: computed on
    # every construction (#94), None when `type` is unknown or missing.
    model: str | None = field(init=False)
    # Curated mounting class for `type` ("cabinet" DIN rail / "wall" /
    # "flush" in-box), derived exactly like `model` (#115). Decoration for
    # device info only - never a topology input. None when unclassified.
    mounting: Mounting | None = field(init=False)
    sw_version: int | None = None  # wersja_softu
    hw_version: int | None = None  # wersja_pcb
    # Designer module-level location (the "Lokalizacja" set on the module
    # itself - where the box is mounted, not where its loads are), read
    # from the DEVICE_NAME entry of the module's CAN description record by
    # :meth:`AmpioClient.resolve_locations` - admin tier only. None until
    # a sweep covers the module, or when the installer never set one.
    location: str | None = None
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", module_model(self.type))
        object.__setattr__(self, "mounting", module_mounting(self.type))


@dataclass(slots=True, frozen=True)
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
    def key(self) -> str:
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
        :meth:`AmpioClient.test_connection` result; a running client's
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
    `started_at` and `reconnect_count` cover the current ``start()`` run -
    a deliberate stop/start restarts them, so a snapshot never reads a
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
