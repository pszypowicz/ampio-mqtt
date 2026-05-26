"""Ampio module type code to model name mapping.

The `typ_urzadzenia` field reported for each module is a numeric hardware type
code. This table resolves it to the human model name (for example 44 -> M-SENS).

The mapping is derived from the device type table published by Ampio in
`node-red-contrib-ampio` (file `ampioin/db/devtypes.json`).

  Copyright (c) 2019, Ampio Sp. z o.o.
  Licensed under the ISC license. The full upstream notice is reproduced
  verbatim in the project-root `NOTICES` file.

Node-RED specific "(unsupported)" annotations and placeholder/reserved entries
are omitted, since here the value is only the hardware model name.
"""

from __future__ import annotations

MODULE_MODELS: dict[int, str] = {
    0: "VIRTUAL",
    1: "M-IN-4p",
    2: "M-REL-1p",
    3: "M-ROL-4s",
    4: "M-REL-8s",
    5: "M-DIM-8s",
    6: "M-AV-AMP-s",
    8: "M-DOT-4",
    9: "M-DOT-18",
    10: "M-SERV-s",
    11: "M-DOT-9",
    12: "M-OC-4s",
    13: "M-DIM-4s",
    14: "M-INOC-8s",
    15: "M-IN-8s",
    16: "M-RTC-s",
    17: "M-LED-1",
    18: "M-DIM-1s",
    19: "M-RT-s",
    20: "M-RT-s",
    21: "M-RT-s",
    22: "M-RT-s",
    23: "M-RT-s",
    24: "M-REL-2",
    25: "M-CON-s",
    26: "M-INOC-4p",
    27: "M-DOT-15LCD",
    28: "M-IN-AC4s",
    29: "M-IN-AD8s",
    30: "M-CON-IR",
    31: "M-ROL-1",
    32: "M-DOT-6",
    33: "M-DOT-2",
    34: "M-METEO",
    35: "M-CON-CAN-s",
    36: "M-DOT-GEST",
    37: "M-CON-ZWAVE-s",
    38: "M-RDN-5s",
    39: "M-OUT-4s",
    40: "M-IN-11p",
    41: "M-OC-32s",
    42: "M-IN-16s",
    43: "M-CON-DALI-s",
    44: "M-SENS",
    45: "M-SMOG",
    46: "M-Ampio1WGW",
    47: "M-CON-KNX-s",
    48: "MEXL",
    49: "MWRC",
    50: "USBGW",
    51: "M-DOT-M4",
    52: "M-DOT-M14",
    53: "GPS-ALARM",
    54: "M-IN-PTK",
    55: "M-INOC-8s",
    56: "M-AV-MP3-s",
    57: "M-DOT-4-RFID",
    60: "M-DOT-M15",
    61: "M-ROOM-s",
    62: "M-REL-10s",
    63: "M-CON-ENOCN-p",
    64: "M-DOT-M4",
    65: "M-ALARM-8s",
    66: "M-ALARM-8s",
    67: "M-CON-WL-p",
    68: "M-DOT-M18",
    69: "M-DIM-2s",
    70: "M-IN-2p",
    71: "RF-MESH",
    72: "M-REL-C4s",
    73: "M-DOT-R14",
    74: "M-CON-KEY-p",
    75: "M-CON-WZ-p",
    76: "M-CON-HVAC-p",
    77: "M-IN-IMP-4s",
    78: "M-DOT-T6",
    79: "M-DOT-R PRO",
    80: "M-DOT-R2 PRO",
}


def module_model(type_code: int | None) -> str | None:
    """Resolve a module type code to its model name, or None if unknown."""
    if type_code is None:
        return None
    return MODULE_MODELS.get(type_code)
