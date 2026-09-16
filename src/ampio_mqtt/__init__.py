"""Async client for the Ampio Smart Home MQTT (DB-object) protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Static visibility for the lazily served names below, so type
    # checkers accept them in `__all__` and resolve their types. The
    # runtime import stays inside `__getattr__`.
    from .discovery import DiscoveryResult, discover

from .classification import (
    INPUT_KIND_KEYS,
    OUTPUT_KIND_KEYS,
    SENSOR_KIND_KEY_PREFIXES,
    SENSOR_KIND_KEYS,
    THERMOSTAT_KIND_KEYS,
    InputKind,
    ObjectKind,
    OutputKind,
    SensorKind,
    ThermostatKind,
)
from .client import HEATING_MODES, MAX_PANEL_FIELD, AmpioClient
from .errors import (
    AmpioAuthError,
    AmpioConnectionError,
    AmpioError,
    AmpioProtocolError,
    AmpioTimeoutError,
    AmpioValueError,
)
from .events import (
    AuthFailed,
    AvailabilityChanged,
    BusEventRaised,
    ClientEvent,
    ConnectionDied,
    ModuleRemoved,
    ModuleUpdated,
    ObjectAdded,
    ObjectRemoved,
    ObjectUpdated,
    PresenceChanged,
)
from .models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    CoverParameters,
    DesignerRecord,
    ModuleFunction,
    ModuleRecord,
    PanelLightSignal,
    PanelSettings,
    PresenceDetection,
    PresenceSimulation,
    RecordSweep,
    ThermostatState,
)

__all__ = [
    "HEATING_MODES",
    "INPUT_KIND_KEYS",
    "MAX_PANEL_FIELD",
    "OUTPUT_KIND_KEYS",
    "SENSOR_KIND_KEYS",
    "SENSOR_KIND_KEY_PREFIXES",
    "THERMOSTAT_KIND_KEYS",
    "AccessTier",
    "AmpioAuthError",
    "AmpioClient",
    "AmpioConnectionError",
    "AmpioError",
    "AmpioModule",
    "AmpioObject",
    "AmpioProtocolError",
    "AmpioScene",
    "AmpioServerInfo",
    "AmpioTimeoutError",
    "AmpioValueError",
    "AuthFailed",
    "AvailabilityChanged",
    "BusEventRaised",
    "ClientEvent",
    "ConnectionDied",
    "CoverParameters",
    "DesignerRecord",
    "DiscoveryResult",
    "InputKind",
    "ModuleFunction",
    "ModuleRecord",
    "ModuleRemoved",
    "ModuleUpdated",
    "ObjectAdded",
    "ObjectKind",
    "ObjectRemoved",
    "ObjectUpdated",
    "OutputKind",
    "PanelLightSignal",
    "PanelSettings",
    "PresenceChanged",
    "PresenceDetection",
    "PresenceSimulation",
    "RecordSweep",
    "SensorKind",
    "ThermostatKind",
    "ThermostatState",
    "discover",
]

__version__ = "0.70.1"


def __getattr__(name: str) -> object:
    """Load the discovery surface lazily: it needs the optional zeroconf
    dependency (the ``ampio-mqtt[discovery]`` extra), and importing the
    package must not."""
    if name in ("discover", "DiscoveryResult"):
        from . import discovery

        return getattr(discovery, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
