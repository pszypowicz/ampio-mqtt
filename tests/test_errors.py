"""Tests for errors raised by ampio_mqtt."""

from __future__ import annotations

from ampio_mqtt import AmpioError, AmpioNotConfigured, AmpioUnsupported


def test_not_configured_names_every_row_for_the_installer() -> None:
    err = AmpioNotConfigured(leafless=((5, "Lamp"), (9, None)))
    assert isinstance(err, AmpioError)
    assert err.leafless == ((5, "Lamp"), (9, None))
    assert "5 (Lamp)" in str(err)
    assert "9" in str(err)


def test_not_configured_names_each_unrecognized_row_apart() -> None:
    err = AmpioNotConfigured(leafless=((5, "Lamp"),), unrecognized=((7, "Relay"),))
    message = str(err)
    assert err.unrecognized == ((7, "Relay"),)
    assert "5 (Lamp) carry no leaf" in message
    assert "7 (Relay)" in message
    assert "7 (Relay) carry no leaf" not in message


def test_not_configured_names_a_shared_mac() -> None:
    err = AmpioNotConfigured(collisions=((0xBE82, (1, 2)),))
    assert err.leafless == () and err.unrecognized == ()
    assert "0xBE82" in str(err) and "1, 2" in str(err)


def test_not_configured_names_a_mac_no_module_row_carries() -> None:
    err = AmpioNotConfigured(collisions=((0xBE82, ()),))
    message = str(err)
    assert err.collisions == ((0xBE82, ()),)
    assert "No Ampio module row carries the override mac 0xBE82" in message
    assert "share" not in message
    assert "  " not in message


def test_unsupported_is_an_ampio_error_and_not_a_value_error() -> None:
    err = AmpioUnsupported("the module drops the lock")
    assert isinstance(err, AmpioError)
    assert not isinstance(err, ValueError)
