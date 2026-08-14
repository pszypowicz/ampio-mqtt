"""Ampio module type code to model name + capability mapping.

The `typ_urzadzenia` field reported for each module is a numeric hardware
type code. This module resolves it to:

- the human model name (for example 44 -> M-SENS) via :func:`module_model`,
- a set of `Capability` flags (digital outputs, analog inputs, RGBW outputs,
  env sensors, UI panel, bridge, ...) via :func:`module_capabilities`.

Both tables are derived from the device-type catalogue published by Ampio
in `node-red-contrib-ampio` (file `ampioin/db/devtypes.json`), a verbatim
copy of which is vendored alongside this module as ``_devtypes.json``.

  Copyright (c) 2019, Ampio Sp. z o.o.
  Licensed under the ISC license. The full upstream notice is reproduced
  verbatim in the project-root `NOTICES` file.

Capability flags are computed at import time from upstream's
``inoptions`` / ``outoptions`` capability codes, plus the model-name prefix
for non-physical dimensions (UI panel, bridge, hub, alarm, AV). The
capability set is the honest abstraction: many modules carry more than one
- an M-REL-* relay board has both `DIGITAL_OUTPUT` and `DIGITAL_INPUT`; an
M-OC-4s has `DIGITAL_OUTPUT`, `ANALOG_INPUT`, and `RGBW_OUTPUT`. A single
label would discard most of the picture.

Consumers (typically a Home Assistant integration) read
``client.modules[id].capabilities`` to decide things like which HA platform
each child object maps to, and whether to bundle a module's children into
one HA device or split each into its own device linked via ``via_device``.
The library exposes the *facts*; the integration applies the *policy*.
"""

from __future__ import annotations

import json
from enum import StrEnum
from importlib.resources import files
from typing import Any

# --- Capability enum ------------------------------------------------------


class Capability(StrEnum):
    """One physical or role-shaped capability advertised by a module."""

    # Physical I/O - derived from upstream inoptions / outoptions codes.
    DIGITAL_OUTPUT = "digital_output"  # 's' in outoptions: drives on/off loads
    DIGITAL_INPUT = "digital_input"  # 'i' in inoptions: button / dry contact
    ANALOG_INPUT = "analog_input"  # 'a'/'ai'/'au'/'au16'/'au32' in inoptions
    TEMPERATURE_INPUT = "temperature_input"  # 't' in inoptions: 1-wire probe
    ENV_SENSOR = "env_sensor"  # hum/absp/relp/db/lux/iaq/co2/temp in inoptions
    ROLLER_OUTPUT = "roller_output"  # 'rs'/'rsdn'/'rm' in outoptions
    RGBW_OUTPUT = "rgbw_output"  # 'rgbw' in inoptions (upstream quirk)
    IR_OUTPUT = "ir_output"  # 'ir' in outoptions
    # Module role hints - derived from the model-name prefix.
    UI_PANEL = "ui_panel"  # M-DOT-* / M-ROOM-* (touch panel)
    BRIDGE = "bridge"  # M-CON-* (DALI / KNX / Z-Wave / EnOcean / ...)
    HUB = "hub"  # M-SERV-* / VIRTUAL
    ALARM = "alarm"  # M-ALARM-*
    AUDIO_VIDEO = "audio_video"  # M-AV-*


# Upstream capability codes grouped into the flags above.
_ENV_SENSOR_CODES = frozenset(
    {"hum", "absp", "relp", "db", "lux", "iaq", "co2", "temp"}
)
_ANALOG_INPUT_CODES = frozenset({"a", "ai", "au", "au16", "au32"})
_ROLLER_OUTPUT_CODES = frozenset({"rs", "rsdn", "rm"})


def _capabilities_for(entry: dict[str, Any]) -> frozenset[Capability]:
    """Compute the Capability flag set for one upstream devtypes entry."""
    name: str = entry.get("type", "")
    inopts = set(entry.get("inoptions") or [])
    outopts = set(entry.get("outoptions") or [])
    caps: set[Capability] = set()

    if "s" in outopts:
        caps.add(Capability.DIGITAL_OUTPUT)
    if "i" in inopts:
        caps.add(Capability.DIGITAL_INPUT)
    if inopts & _ANALOG_INPUT_CODES:
        caps.add(Capability.ANALOG_INPUT)
    if "t" in inopts:
        caps.add(Capability.TEMPERATURE_INPUT)
    if inopts & _ENV_SENSOR_CODES:
        caps.add(Capability.ENV_SENSOR)
    if outopts & _ROLLER_OUTPUT_CODES:
        caps.add(Capability.ROLLER_OUTPUT)
    if "rgbw" in inopts:
        caps.add(Capability.RGBW_OUTPUT)
    if "ir" in outopts:
        caps.add(Capability.IR_OUTPUT)

    if name.startswith(("M-DOT-", "M-ROOM-")):
        caps.add(Capability.UI_PANEL)
    if name.startswith("M-CON-"):
        caps.add(Capability.BRIDGE)
    if name.startswith("M-SERV-") or name == "VIRTUAL":
        caps.add(Capability.HUB)
    if name.startswith("M-ALARM-"):
        caps.add(Capability.ALARM)
    if name.startswith("M-AV-"):
        caps.add(Capability.AUDIO_VIDEO)

    return frozenset(caps)


# --- Catalogue (built once at import time from the vendored JSON) --------


def _load_upstream_devtypes() -> list[dict[str, Any]]:
    """Read the vendored ``_devtypes.json`` shipped with the package."""
    payload = files(__package__).joinpath("_devtypes.json").read_text(encoding="utf-8")
    data = json.loads(payload)
    if not isinstance(data, list):
        raise TypeError("_devtypes.json: top-level must be a list")
    return data


def _build_catalogue() -> tuple[dict[int, str], dict[int, frozenset[Capability]]]:
    models: dict[int, str] = {}
    caps: dict[int, frozenset[Capability]] = {}
    for entry in _load_upstream_devtypes():
        value = entry.get("value")
        name = entry.get("type")
        if not isinstance(value, int) or not isinstance(name, str):
            continue
        models[value] = name
        caps[value] = _capabilities_for(entry)
    return models, caps


MODULE_MODELS, MODULE_CAPABILITIES = _build_catalogue()


def module_model(type_code: int | None) -> str | None:
    """Resolve a module type code to its model name, or None if unknown."""
    if type_code is None:
        return None
    return MODULE_MODELS.get(type_code)


def module_capabilities(type_code: int | None) -> frozenset[Capability] | None:
    """Resolve a module type code to its `Capability` set, or None if unknown.

    Returns an empty ``frozenset`` for *known* types that don't advertise any
    of our modeled capabilities (rare, but happens for sparsely-described
    upstream entries like ``M-METEO``/``M-SMOG``). Returns ``None`` only when
    the type code itself is unknown - consumers can distinguish "module is
    known but has no flags" from "module is unknown".
    """
    if type_code is None:
        return None
    return MODULE_CAPABILITIES.get(type_code)
