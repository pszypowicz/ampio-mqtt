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
    classify,
)
from .client import AmpioClient
from .device_types import Capability, module_capabilities, module_model
from .discovery import DiscoveryResult, discover
from .endpoints import AccessTier
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
    "Capability",
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
    "classify",
    "discover",
    "module_capabilities",
    "module_model",
]

__version__ = "0.21.0"
