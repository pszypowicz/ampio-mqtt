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


class AmpioProtocolError(AmpioError):
    """Raised when a reply lacks something its surface always serves.

    The account tier fixes which surface answers, and each surface serves a
    fixed column set (docs/protocol.md). A reply that drops a column, or
    that is not the surface's own document shape, is a server fault. To
    read it as an unconfigured object would hide that fault behind wrong
    values, so the parse refuses it instead. The client reports the refusal
    through ``diagnostics_snapshot()`` and keeps the connection up.
    """
