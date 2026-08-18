"""Ampio module type code to model name mapping.

The `typ_urzadzenia` field reported for each module is a numeric hardware
type code. :func:`module_model` resolves it to the human model name (for
example 44 -> M-SENS) for a consumer's device info, and :func:`is_hub`
answers the one capability question the library itself has - which module
is the M-SERV - for :pyattr:`AmpioClient.mserv_id`.

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
from typing import Any


def _load_upstream_devtypes() -> list[dict[str, Any]]:
    """Read the vendored ``_devtypes.json`` shipped with the package."""
    payload = files(__package__).joinpath("_devtypes.json").read_text(encoding="utf-8")
    data = json.loads(payload)
    if not isinstance(data, list):
        raise TypeError("_devtypes.json: top-level must be a list")
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


def is_hub(type_code: int | None) -> bool:
    """Whether a module type code is an M-SERV-class hub.

    The upstream catalogue marks the hub role only through the model name:
    the M-SERV variants share the ``M-SERV-`` prefix, and ``VIRTUAL`` is
    the M-SERV's own virtual-module face.
    """
    model = module_model(type_code)
    return model is not None and (model.startswith("M-SERV-") or model == "VIRTUAL")
