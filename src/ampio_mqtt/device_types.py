"""Ampio module type code to model name mapping.

The `typ_urzadzenia` field reported for each module is a numeric hardware
type code. :func:`module_model` resolves it to the human model name (for
example 44 -> M-SENS) for a consumer's device info, and :func:`is_hub`
answers the one capability question the library itself has - which module
is the M-SERV - for :pyattr:`AmpioClient.mserv`.

The table is derived from the device-type catalogue published by Ampio
in `node-red-contrib-ampio` (file `ampioin/db/devtypes.json`), a verbatim
copy of which is vendored alongside this module as ``_devtypes.json``.

  Copyright (c) 2019, Ampio Sp. z o.o.
  Licensed under the ISC license. The full upstream notice is reproduced
  verbatim in the project-root `NOTICES` file.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any, Literal

# Where a module is physically mounted: on the DIN rail of a distribution
# cabinet, on a wall (panels, sensors, outdoor field devices), or inside
# an in-wall installation box behind a switch or socket.
Mounting = Literal["cabinet", "wall", "flush"]


def _load_upstream_devtypes() -> list[dict[str, Any]]:
    """Read the vendored ``_devtypes.json`` shipped with the package."""
    payload = files(__package__).joinpath("_devtypes.json").read_text(encoding="utf-8")
    data: list[dict[str, Any]] = json.loads(payload)
    return data


def _build_catalogue() -> dict[int, str]:
    models: dict[int, str] = {}
    for entry in _load_upstream_devtypes():
        value = entry.get("value")
        name = entry.get("type")
        if isinstance(value, int) and isinstance(name, str):
            models[value] = name
    return models


MODULE_MODELS = _build_catalogue()


def module_model(type_code: int | None) -> str | None:
    """Resolve a module type code to its model name, or None if unknown."""
    if type_code is None:
        return None
    return MODULE_MODELS.get(type_code)


# Curated mounting class per type code (#115): the Ampio naming convention
# (-s = DIN rail, -p = flush box, M-DOT* / M-SENS / M-METEO = wall) plus
# per-product judgment where the name carries no suffix. Decoration for a
# consumer's device info only - the device topology never branches on it
# (#114 pins that contract). A code absent here (virtual, bridge-only,
# handheld, reserved, or unknown) resolves to None.
MODULE_MOUNTING: dict[int, Mounting] = {
    1: "flush",  # M-IN-4p
    2: "flush",  # M-REL-1p
    3: "cabinet",  # M-ROL-4s
    4: "cabinet",  # M-REL-8s
    5: "cabinet",  # M-DIM-8s
    6: "cabinet",  # M-AV-AMP-s
    8: "wall",  # M-DOT-4
    9: "wall",  # M-DOT-18
    10: "cabinet",  # M-SERV-s
    11: "wall",  # M-DOT-9
    12: "cabinet",  # M-OC-4s
    13: "cabinet",  # M-DIM-4s
    14: "cabinet",  # M-INOC-8s
    15: "cabinet",  # M-IN-8s
    16: "cabinet",  # M-RTC-s
    17: "cabinet",  # M-LED-1
    18: "cabinet",  # M-DIM-1s
    19: "cabinet",  # M-RT-s
    20: "cabinet",  # M-RT-s
    21: "cabinet",  # M-RT-s
    22: "cabinet",  # M-RT-s
    23: "cabinet",  # M-RT-s
    24: "flush",  # M-REL-2 (the mini in-box relay)
    25: "cabinet",  # M-CON-s
    26: "flush",  # M-INOC-4p
    27: "wall",  # M-DOT-15LCD
    28: "cabinet",  # M-IN-AC4s
    29: "cabinet",  # M-IN-AD8s
    31: "flush",  # M-ROL-1 (in-box, at the roller)
    32: "wall",  # M-DOT-6
    33: "wall",  # M-DOT-2
    34: "wall",  # M-METEO (outdoor field device)
    35: "cabinet",  # M-CON-CAN-s
    36: "wall",  # M-DOT-GEST
    37: "cabinet",  # M-CON-ZWAVE-s
    38: "cabinet",  # M-RDN-5s
    39: "cabinet",  # M-OUT-4s
    40: "flush",  # M-IN-11p
    41: "cabinet",  # M-OC-32s
    42: "cabinet",  # M-IN-16s
    43: "cabinet",  # M-CON-DALI-s
    44: "wall",  # M-SENS
    45: "wall",  # M-SMOG (outdoor field device)
    47: "cabinet",  # M-CON-KNX-s
    51: "wall",  # M-DOT-M4
    52: "wall",  # M-DOT-M14
    55: "cabinet",  # M-INOC-8s
    56: "cabinet",  # M-AV-MP3-s
    57: "wall",  # M-DOT-4-RFID
    60: "wall",  # M-DOT-M15
    61: "cabinet",  # M-ROOM-s (the -s suffix; a per-room DIN enclosure)
    62: "cabinet",  # M-REL-10s
    63: "flush",  # M-CON-ENOCN-p
    64: "wall",  # M-DOT-M4
    65: "cabinet",  # M-ALARM-8s
    66: "cabinet",  # M-ALARM-8s
    67: "flush",  # M-CON-WL-p
    68: "wall",  # M-DOT-M18
    69: "cabinet",  # M-DIM-2s
    70: "flush",  # M-IN-2p
    72: "cabinet",  # M-REL-C4s
    73: "wall",  # M-DOT-R14
    74: "flush",  # M-CON-KEY-p
    75: "flush",  # M-CON-WZ-p
    76: "flush",  # M-CON-HVAC-p
    77: "cabinet",  # M-IN-IMP-4s
    78: "wall",  # M-DOT-T6
    79: "wall",  # M-DOT-R PRO
    80: "wall",  # M-DOT-R2 PRO
}


def module_mounting(type_code: int | None) -> Mounting | None:
    """Resolve a module type code to its mounting class, or None."""
    if type_code is None:
        return None
    return MODULE_MOUNTING.get(type_code)


def is_hub(type_code: int | None) -> bool:
    """Whether a module type code is an M-SERV-class hub.

    The upstream catalogue marks the hub role only through the model name:
    the M-SERV variants share the ``M-SERV-`` prefix, and ``VIRTUAL`` is
    the M-SERV's own virtual-module face.
    """
    model = module_model(type_code)
    return model is not None and (model.startswith("M-SERV-") or model == "VIRTUAL")
