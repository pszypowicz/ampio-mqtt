"""Async client for the Ampio Smart Home MQTT (DB-object) protocol."""

from __future__ import annotations

from .client import AmpioClient
from .const import InputKind, SensorKind, classify
from .device_types import Capability, module_capabilities, module_model
from .discovery import DiscoveryResult, discover
from .errors import AmpioAuthError, AmpioConnectionError, AmpioError
from .models import (
    AmpioModule,
    AmpioObject,
    AmpioServerInfo,
    AmpioState,
    ConnectionStats,
)

__all__ = [
    "AmpioAuthError",
    "AmpioClient",
    "AmpioConnectionError",
    "AmpioError",
    "AmpioModule",
    "AmpioObject",
    "AmpioServerInfo",
    "AmpioState",
    "Capability",
    "ConnectionStats",
    "DiscoveryResult",
    "InputKind",
    "SensorKind",
    "classify",
    "discover",
    "module_capabilities",
    "module_model",
]

__version__ = "0.6.0"
