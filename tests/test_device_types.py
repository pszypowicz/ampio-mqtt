"""Tests for module type code -> model name resolution and hub detection."""

from __future__ import annotations

import pytest

from ampio_mqtt.device_types import is_hub, module_model

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


# --- is_hub ---------------------------------------------------------------


@pytest.mark.parametrize("code", [10, 0])  # M-SERV-s, VIRTUAL
def test_hub_types(code: int) -> None:
    assert is_hub(code) is True


@pytest.mark.parametrize("code", [4, 44, 999, None])  # M-REL-8s, M-SENS, unknown
def test_non_hub_types(code: int | None) -> None:
    assert is_hub(code) is False
