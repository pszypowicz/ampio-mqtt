"""Tests for errors raised by ampio_mqtt."""

from __future__ import annotations

from ampio_mqtt import AmpioError, AmpioNotConfigured, AmpioUnsupported


def test_not_configured_names_every_row_for_the_installer() -> None:
    err = AmpioNotConfigured(((5, "Lamp"), (9, None)))
    assert isinstance(err, AmpioError)
    assert err.objects == ((5, "Lamp"), (9, None))
    assert "5 (Lamp)" in str(err)
    assert "9" in str(err)


def test_not_configured_names_a_shared_mac() -> None:
    err = AmpioNotConfigured(collisions=((0xBE82, (1, 2)),))
    assert err.objects == ()
    assert "be82" in str(err) and "1, 2" in str(err)


def test_unsupported_is_an_ampio_error_and_not_a_value_error() -> None:
    err = AmpioUnsupported("the module drops the lock")
    assert isinstance(err, AmpioError)
    assert not isinstance(err, ValueError)
