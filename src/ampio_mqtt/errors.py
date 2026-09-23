"""Exceptions raised by ampio_mqtt."""

from __future__ import annotations

from .models import format_mac


class AmpioError(Exception):
    """Base error."""


class AmpioConnectionError(AmpioError):
    """Raised when the broker connection fails or is not up.

    A credential rejection during connection setup raises
    :class:`AmpioAuthError` instead. A publish wraps every MQTT error in
    this class.
    """


class AmpioTimeoutError(AmpioConnectionError):
    """Raised when an expected reply or acknowledgement does not arrive in time.

    Also raised when ``check_connection`` receives an unreadable server-info
    reply.

    Subclasses ``AmpioConnectionError`` so a handler that treats every
    connection problem alike keeps working; catch this one first to tell "the
    server did not answer in time, try again" apart from a transport failure.
    """


class AmpioAuthError(AmpioError):
    """Raised when the broker rejects the credentials."""


class AmpioNotConfigured(AmpioError):
    """Raised when the Designer configuration leaves a row unaddressable.

    A drivable object row carries no leaf: Designer clears the leaf when
    an object's Matter box is checked and then unchecked, and the library
    addresses an object on the bus through its leaf alone, so ``objects``
    names every such row as an ``(id, name)`` pair.

    ``collisions`` names each mac no single module row carries, with the
    ids of the rows on it. Two or more ids are the rows that share the
    mac, which the raw tree keys on and cannot attribute a frame to. An
    empty tuple is a mac the module list carries no row for, so a write
    addressed by that mac reaches no module.

    The installer fixes each in Designer. The connection stays up, and
    every other row is served.
    """

    def __init__(
        self,
        objects: tuple[tuple[int, str | None], ...] = (),
        collisions: tuple[tuple[int, tuple[int, ...]], ...] = (),
    ) -> None:
        self.objects = objects
        self.collisions = collisions
        parts: list[str] = []
        if objects:
            listed = ", ".join(
                f"{oid} ({name})" if name else str(oid) for oid, name in objects
            )
            parts.append(
                f"Ampio object(s) {listed} carry no leaf. Designer clears the leaf "
                "when the Matter box is checked and unchecked. Restore each in "
                "Designer and save"
            )
        for mac, ids in collisions:
            if ids:
                parts.append(
                    f"Ampio modules {', '.join(map(str, ids))} share the override "
                    f"mac {format_mac(mac)}. Give each module its own mac in "
                    "Designer and save"
                )
            else:
                parts.append(
                    f"No Ampio module row carries the override mac {format_mac(mac)}. "
                    "Add the module in Designer, or remove the objects that name it, "
                    "and save"
                )
        super().__init__(". ".join(parts))


class AmpioValueError(AmpioError, ValueError):
    """Raised when the call is the caller's fault.

    A value beyond the range the frame carries, a mis-typed argument, an
    unlisted heating mode, an id the catalogue does not list, or a call
    that needs a sweep that did not run. ``ValueError`` is a base because
    that is what a bad argument is in Python, so a handler that catches
    the builtin keeps working. What the install cannot do raises
    :class:`AmpioUnsupported` instead.
    """


class AmpioUnsupported(AmpioError):
    """Raised when the install cannot do what the call asks.

    The call is well formed and the object is in the catalogue, and the
    refusal comes from what the install is: an output whose kind does not
    answer the verb, a kind no timed write pulses, a lock call on a
    non-cover, a module that advertises no roller channel count. Nobody
    fixes it, so a consumer leaves the control out instead of catching this.
    """


class AmpioProtocolError(AmpioError):
    """Raised when a reply lacks something its surface always serves.

    The account tier fixes which surface answers, and each surface serves a
    fixed column set (docs/protocol.md). A reply that drops a column, or
    that is not the surface's own document shape, is a server fault. To
    read it as an unconfigured object would hide that fault behind wrong
    values, so the parse refuses it instead. The client reports the refusal
    through ``diagnostics_snapshot()`` and keeps the connection up.
    """
