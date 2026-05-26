"""Async client for the Ampio Smart Home MQTT (DB-object) protocol."""

from __future__ import annotations

from .client import (
    AmpioAuthError,
    AmpioClient,
    AmpioConnectionError,
    AmpioError,
)
from .const import DeviceClass, SensorKind, StateClass, classify_object
from .device_types import module_model
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
    "DeviceClass",
    "SensorKind",
    "StateClass",
    "classify_object",
    "module_model",
]

__version__ = "0.1.0"
