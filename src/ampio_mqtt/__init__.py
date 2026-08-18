"""Async client for the Ampio Smart Home MQTT (DB-object) protocol."""

from __future__ import annotations

from .classification import (
    INPUT_KIND_KEYS,
    OPEN_SENSOR_KEY_PREFIXES,
    OUTPUT_KIND_KEYS,
    SENSOR_KIND_KEYS,
    THERMOSTAT_KIND_KEYS,
    InputKind,
    ObjectKind,
    OutputKind,
    SensorKind,
    ThermostatKind,
)
from .client import AmpioClient
from .discovery import DiscoveryResult, discover
from .errors import (
    AmpioAuthError,
    AmpioConnectionError,
    AmpioError,
    AmpioTimeoutError,
)
from .events import (
    AuthFailed,
    AvailabilityChanged,
    BusEvent,
    ClientEvent,
    ConnectionDied,
    ModuleRemoved,
    ModuleUpdated,
    ObjectRemoved,
    ObjectUpdated,
)
from .models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioScene,
    AmpioServerInfo,
    ConnectionStats,
)

__all__ = [
    "INPUT_KIND_KEYS",
    "OPEN_SENSOR_KEY_PREFIXES",
    "OUTPUT_KIND_KEYS",
    "SENSOR_KIND_KEYS",
    "THERMOSTAT_KIND_KEYS",
    "AccessTier",
    "AmpioAuthError",
    "AmpioClient",
    "AmpioConnectionError",
    "AmpioError",
    "AmpioModule",
    "AmpioObject",
    "AmpioScene",
    "AmpioServerInfo",
    "AmpioTimeoutError",
    "AuthFailed",
    "AvailabilityChanged",
    "BusEvent",
    "ClientEvent",
    "ConnectionDied",
    "ConnectionStats",
    "DiscoveryResult",
    "InputKind",
    "ModuleRemoved",
    "ModuleUpdated",
    "ObjectKind",
    "ObjectRemoved",
    "ObjectUpdated",
    "OutputKind",
    "SensorKind",
    "ThermostatKind",
    "discover",
]

__version__ = "0.22.0"
