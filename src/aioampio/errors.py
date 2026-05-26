"""Exceptions raised by aioampio."""

from __future__ import annotations


class AmpioError(Exception):
    """Base error."""


class AmpioConnectionError(AmpioError):
    """Raised when the broker connection fails for non-auth reasons."""


class AmpioAuthError(AmpioConnectionError):
    """Raised when the broker rejects the credentials."""
