"""Data models for Ampio DB objects."""

from __future__ import annotations

from dataclasses import dataclass, field

from .const import SensorKind


@dataclass(slots=True)
class AmpioObject:
    """A logical Ampio object (DB object) and its latest state."""

    id: int
    device_id: int | None = None  # id_urzadzenia (physical module)
    typ_komponentu: str | None = None
    name: str | None = None
    interpretacja: int | None = None
    funkcja: int | None = None
    room_id: int | None = None  # lokalizacja
    min: float | None = None
    max: float | None = None
    kind: SensorKind | None = None  # set when classified as a sensor
    value: str | None = None
    desc: str | None = None

    @property
    def is_sensor(self) -> bool:
        """Whether this object is exposed by the sensor platform."""
        return self.kind is not None


@dataclass(slots=True)
class AmpioModule:
    """A physical Ampio module (urzadzenie) that owns objects."""

    id: int
    mac: int | None = None
    name: str | None = None  # nazwa_urzadzenia (user-given module name)
    type: int | None = None  # typ_urzadzenia
    model: str | None = None  # resolved model name for `type`
    sw_version: int | None = None  # wersja_softu
    hw_version: int | None = None  # wersja_pcb
    # Epoch seconds of the last state message received for any of the module's
    # objects. Source: the `on` field of the state payload (milliseconds epoch
    # at the server), falling back to local receive time if absent.
    last_seen: float | None = None


@dataclass(slots=True)
class AmpioState:
    """All known objects and modules, keyed by id."""

    objects: dict[int, AmpioObject] = field(default_factory=dict)
    modules: dict[int, AmpioModule] = field(default_factory=dict)

    @property
    def sensors(self) -> dict[int, AmpioObject]:
        """Objects classified as sensors."""
        return {i: o for i, o in self.objects.items() if o.is_sensor}
