"""Async client for the Ampio Smart Home MQTT (DB-object) protocol."""

from __future__ import annotations

from .classification import (
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
