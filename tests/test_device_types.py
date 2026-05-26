"""Tests for module type code to model name resolution."""

from __future__ import annotations

from ampio_mqtt import module_model
import pytest


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
