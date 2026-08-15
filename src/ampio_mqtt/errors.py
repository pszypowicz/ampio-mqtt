"""Exceptions raised by ampio_mqtt."""

from __future__ import annotations


class AmpioError(Exception):
    """Base error."""


class AmpioConnectionError(AmpioError):
    """Raised when the broker connection fails for non-auth transport reasons."""


class AmpioTimeoutError(AmpioConnectionError):
    """Raised when the broker is reachable but an expected reply never arrives.

    Subclasses ``AmpioConnectionError`` so a handler that treats every
    connection problem alike keeps working; catch this one first to tell "the
    server did not answer in time, try again" apart from a transport failure.
    """


class AmpioAuthError(AmpioError):
    """Raised when the broker rejects the credentials."""
