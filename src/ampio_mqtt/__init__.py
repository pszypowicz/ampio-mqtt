"""Async client for the Ampio Smart Home MQTT (DB-object) protocol."""

from __future__ import annotations

from .client import AmpioClient
from .const import SensorKind, classify_object
from .device_types import module_model
from .discovery import DiscoveryResult, discover
from .errors import AmpioAuthError, AmpioConnectionError, AmpioError
from .models import AmpioModule, AmpioObject, AmpioServerInfo, AmpioState

__all__ = [
    "AmpioAuthError",
    "AmpioClient",
    "AmpioConnectionError",
    "AmpioError",
    "AmpioModule",
    "AmpioObject",
    "AmpioServerInfo",
    "AmpioState",
    "DiscoveryResult",
    "SensorKind",
    "classify_object",
    "discover",
    "module_model",
]

__version__ = "1.2.0"
