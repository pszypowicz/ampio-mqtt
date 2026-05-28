"""Data models for Ampio DB objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from .const import InputKind, SensorKind
from .device_types import Capability


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
    kind: SensorKind | None = None  # set when classified as a sensor
    input_kind: InputKind | None = None  # set when classified as an input
    value: str | None = None

    @property
    def is_sensor(self) -> bool:
        """Whether this object is exposed by the sensor platform."""
        return self.kind is not None

    @property
    def is_input(self) -> bool:
        """Whether this object is exposed by the binary_sensor/input platform."""
        return self.input_kind is not None

    @property
    def is_on(self) -> bool:
        """Boolean interpretation of `value`, meaningful for input objects.

        Off when `value` is None/empty/`"0"`, on otherwise - so both the raw
        channel form (`"1"`) and the per-object form (`"255"`) read as on.
        """
        return self.value not in (None, "", "0")


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
    # Epoch seconds of the last state message received for any of the module's
    # objects. Source: the `on` field of the state payload (milliseconds epoch
    # at the server), falling back to local receive time if absent.
    last_seen: float | None = None


@dataclass(slots=True)
class AmpioServerInfo:
    """A safe subset of the Ampio M-SERV self-reported info.

    Intentionally excludes fields that would leak private data
    (geolocation, cloud endpoint, public key, user permissions).
    """

    mac: int | None = None  # the M-SERV's own CAN mac (matches a module's mac_global)
    server_version: str | None = None  # ampio_mqtt application version
    server_revision: str | None = None
    mqtt_version: str | None = None  # broker version
    local_ip: str | None = None  # used for the configuration_url
    device_id: str | None = None  # hardware identifier of the host


@dataclass(slots=True)
class AmpioState:
    """All known objects, modules, and server info."""

    objects: dict[int, AmpioObject] = field(default_factory=dict)
    modules: dict[int, AmpioModule] = field(default_factory=dict)
    server_info: AmpioServerInfo | None = None

    @property
    def sensors(self) -> dict[int, AmpioObject]:
        """Objects classified as sensors."""
        return {i: o for i, o in self.objects.items() if o.is_sensor}
