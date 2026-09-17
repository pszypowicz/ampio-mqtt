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


class AmpioNotConfigured(AmpioError):
    """Raised when the Designer configuration leaves a row unaddressable.

    Two installer faults. A drivable object row carries no leaf: Designer
    clears the leaf when an object's Matter box is checked and then
    unchecked, and the library addresses an object on the bus through its
    leaf alone, so ``objects`` names every such row as an ``(id, name)``
    pair. Two module rows carry one override mac: the raw tree keys on
    that mac and cannot attribute a frame to either row, so ``collisions``
    names each shared mac with the module ids on it. The installer fixes
    both in Designer. The connection stays up, and every other row is
    served.
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
            parts.append(
                f"Ampio modules {', '.join(map(str, ids))} share the override mac "
                f"{mac:x}. Give each module its own mac in Designer and save"
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
    refusal comes from what the hardware or the firmware answers: an
    output whose kind does not answer the verb, a kind no timed write
    pulses, a module generation without the roller lock. Nobody fixes it,
    so a consumer leaves the control out instead of catching this.
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
