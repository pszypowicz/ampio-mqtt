"""Async client for the Ampio Smart Home MQTT (DB-object) protocol."""

from __future__ import annotations

from .classification import (
    InputKind,
    ObjectKind,
    OutputKind,
    SensorKind,
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
from .models import (
    AmpioEvent,
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
    "AmpioEvent",
    "AmpioModule",
    "AmpioObject",
    "AmpioScene",
    "AmpioServerInfo",
    "AmpioTimeoutError",
    "Capability",
    "ConnectionStats",
    "DiscoveryResult",
    "InputKind",
    "ObjectKind",
    "OutputKind",
    "SensorKind",
    "classify",
    "discover",
    "module_capabilities",
    "module_model",
]

__version__ = "0.16.0"
