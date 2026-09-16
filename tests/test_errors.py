"""Tests for errors raised by ampio_mqtt."""

from __future__ import annotations

from ampio_mqtt import AmpioError, AmpioNotConfigured


def test_not_configured_names_every_row_for_the_installer() -> None:
    err = AmpioNotConfigured(((5, "Lamp"), (9, None)))
    assert isinstance(err, AmpioError)
    assert err.objects == ((5, "Lamp"), (9, None))
    assert "5 (Lamp)" in str(err)
    assert "9" in str(err)
