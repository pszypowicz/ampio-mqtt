"""Tests for module type code resolution: the model name and the mounting."""

from __future__ import annotations

import pytest

from ampio_mqtt.device_types import (
    MODULE_MODELS,
    MODULE_MOUNTING,
    module_model,
    module_mounting,
)
from ampio_mqtt.models import AmpioModule

# --- module_model ---------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "model"),
    [
        (44, "M-SENS"),
        (3, "M-ROL-4s"),
        (4, "M-REL-8s"),
        (10, "M-SERV-s"),
        (25, "M-CON-s"),
    ],
)
def test_known_models(code: int, model: str) -> None:
    assert module_model(code) == model


def test_unknown_type_returns_none() -> None:
    assert module_model(999) is None


def test_none_returns_none() -> None:
    assert module_model(None) is None


# --- module_mounting (#115) -------------------------------------------------


@pytest.mark.parametrize(
    ("code", "mounting"),
    [
        (4, "cabinet"),  # M-REL-8s, DIN rail
        (10, "cabinet"),  # M-SERV-s
        (44, "wall"),  # M-SENS
        (11, "wall"),  # M-DOT-9
        (34, "wall"),  # M-METEO, an outdoor field device
        (70, "flush"),  # M-IN-2p, in-box
        (2, "flush"),  # M-REL-1p
        (0, None),  # VIRTUAL
        (49, None),  # MWRC, handheld
        (999, None),  # unknown code
        (None, None),
    ],
)
def test_module_mounting(code: int | None, mounting: str | None) -> None:
    assert module_mounting(code) == mounting


def test_mounting_table_stays_within_the_catalogue() -> None:
    """Every classified code names a catalogued product."""
    assert set(MODULE_MOUNTING) <= set(MODULE_MODELS)


def test_unclassified_codes_are_the_deliberate_set() -> None:
    """The curation tripwire: a catalogue code is classified or deliberately
    out (virtual, bridge-only, handheld, reserved, unknown)."""
    assert set(MODULE_MODELS) - set(MODULE_MOUNTING) == {
        0,  # VIRTUAL
        7,  # RES. (reserved)
        30,  # M-CON-IR
        46,  # M-Ampio1WGW
        48,  # MEXL
        49,  # MWRC (handheld)
        50,  # USBGW
        53,  # GPS-ALARM
        54,  # M-IN-PTK
        58,  # UNKNOWN
        59,  # UNKNOWN
        71,  # RF-MESH
        81,  # TYP81
        82,  # TYP81
        83,  # TYP81
    }


def test_ampio_module_derives_mounting() -> None:
    def module(typ: int) -> AmpioModule:
        return AmpioModule(
            id=1, mac=1, mac_global=2, typ_urzadzenia=typ, wersja_softu=1, wersja_pcb=1
        )

    assert module(44).mounting == "wall"
    assert module(4).mounting == "cabinet"
    assert module(999).mounting is None  # a type code outside the catalogue
