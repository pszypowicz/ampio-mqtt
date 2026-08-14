"""Tests for module type code -> model name + capability resolution."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from ampio_mqtt import Capability, module_capabilities, module_model
from ampio_mqtt.device_types import (
    MODULE_CAPABILITIES,
    MODULE_MODELS,
    _capabilities_for,
)

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


# --- module_capabilities --------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected_subset"),
    [
        # M-SERV: digital I/O + temperature probe + hub role
        (
            10,  # M-SERV-s
            {
                Capability.DIGITAL_OUTPUT,
                Capability.DIGITAL_INPUT,
                Capability.TEMPERATURE_INPUT,
                Capability.HUB,
            },
        ),
        # VIRTUAL has 'a' in inoptions (M-SERV proper does not) - so also analog_input
        (
            0,
            {
                Capability.DIGITAL_OUTPUT,
                Capability.DIGITAL_INPUT,
                Capability.ANALOG_INPUT,
                Capability.TEMPERATURE_INPUT,
                Capability.HUB,
            },
        ),
        # Relay board with feedback inputs - both DIGITAL_OUTPUT and DIGITAL_INPUT
        (
            4,  # M-REL-8s
            {
                Capability.DIGITAL_OUTPUT,
                Capability.DIGITAL_INPUT,
                Capability.TEMPERATURE_INPUT,
            },
        ),
        # M-DOT panel: button inputs + UI role + virtual outputs for scenes
        (
            11,  # M-DOT-9
            {
                Capability.DIGITAL_OUTPUT,
                Capability.DIGITAL_INPUT,
                Capability.TEMPERATURE_INPUT,
                Capability.UI_PANEL,
            },
        ),
        # M-OC-4s: OC drivers + analog inputs + RGBW
        (
            12,
            {
                Capability.DIGITAL_OUTPUT,
                Capability.ANALOG_INPUT,
                Capability.RGBW_OUTPUT,
            },
        ),
        # M-INOC-8s hybrid: digital out + digital in + analog in
        (
            14,
            {
                Capability.DIGITAL_OUTPUT,
                Capability.DIGITAL_INPUT,
                Capability.ANALOG_INPUT,
            },
        ),
        # Pure input panel
        (15, {Capability.DIGITAL_INPUT, Capability.TEMPERATURE_INPUT}),  # M-IN-8s
        # Dedicated roller-shutter head
        (19, {Capability.ROLLER_OUTPUT}),  # M-RT-s
        # Bridge with analog inputs
        (25, {Capability.ANALOG_INPUT, Capability.BRIDGE}),  # M-CON-s
        # Bridge that also drives downstream lights (DALI)
        (
            43,  # M-CON-DALI-s
            {Capability.DIGITAL_OUTPUT, Capability.ANALOG_INPUT, Capability.BRIDGE},
        ),
        # M-SENS: env sensor pack + IR output
        (44, {Capability.ENV_SENSOR, Capability.IR_OUTPUT}),
        # Alarm controller
        (65, {Capability.ALARM}),  # M-ALARM-8s
        # ROOM controller
        (
            61,  # M-ROOM-s
            {
                Capability.DIGITAL_OUTPUT,
                Capability.DIGITAL_INPUT,
                Capability.TEMPERATURE_INPUT,
                Capability.UI_PANEL,
            },
        ),
    ],
)
def test_capability_spot_checks(code: int, expected_subset: set[Capability]) -> None:
    caps = module_capabilities(code)
    assert caps is not None
    assert expected_subset == set(caps), f"caps for typ {code} = {sorted(caps)}"


def test_capabilities_unknown_type_returns_none() -> None:
    assert module_capabilities(999) is None


def test_capabilities_none_returns_none() -> None:
    assert module_capabilities(None) is None


def test_every_known_model_has_a_capability_set() -> None:
    """Every entry in MODULE_MODELS must be in MODULE_CAPABILITIES.

    Possibly an empty frozenset (M-METEO and M-SMOG end up empty per the
    sparsely-described upstream JSON), but the key must exist so consumers
    can distinguish "known model, no flags" from "unknown type".
    """
    for type_code, model in MODULE_MODELS.items():
        caps = MODULE_CAPABILITIES.get(type_code)
        assert caps is not None, f"type {type_code} ({model}) missing capabilities"
        assert isinstance(caps, frozenset)
        for cap in caps:
            assert isinstance(cap, Capability), f"non-Capability {cap!r} for {model}"


def test_module_capabilities_matches_live_rederivation() -> None:
    """MODULE_CAPABILITIES must equal a fresh re-derivation from the JSON.

    Catches the case where someone edits ``MODULE_CAPABILITIES`` by hand or
    edits the algorithm without refreshing the table. The vendored
    ``_devtypes.json`` is the single source of truth.
    """
    payload = files("ampio_mqtt").joinpath("_devtypes.json").read_text(encoding="utf-8")
    rederived: dict[int, frozenset[Capability]] = {}
    for entry in json.loads(payload):
        value = entry.get("value")
        if isinstance(value, int):
            rederived[value] = _capabilities_for(entry)
    assert rederived == MODULE_CAPABILITIES


def test_inoc_modules_are_io_hybrids() -> None:
    """M-INOC-* has BOTH digital outputs and analog inputs."""
    for typ in (14, 26, 55):  # M-INOC-8s x2 + M-INOC-4p
        caps = module_capabilities(typ)
        assert caps is not None
        assert Capability.DIGITAL_OUTPUT in caps
        assert Capability.ANALOG_INPUT in caps


def test_rel_modules_have_inputs_too() -> None:
    """Relay boards advertise digital inputs in upstream's catalogue."""
    for typ in (2, 4, 24, 62, 72):  # M-REL-1p, M-REL-8s, M-REL-2, M-REL-10s, M-REL-C4s
        caps = module_capabilities(typ)
        assert caps is not None
        assert Capability.DIGITAL_OUTPUT in caps
        assert Capability.DIGITAL_INPUT in caps


def test_capability_enum_is_strenum() -> None:
    """Capability values are stable lowercase identifiers (StrEnum)."""
    assert Capability.DIGITAL_OUTPUT == "digital_output"
    assert Capability.RGBW_OUTPUT == "rgbw_output"
    assert Capability.UI_PANEL == "ui_panel"
