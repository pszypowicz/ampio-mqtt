"""Typed events dispatched to :meth:`AmpioClient.subscribe` listeners.

One stream carries everything the library learns: object and module news
from the store, bus events, and connection-state transitions, in the order
they were produced. Every class is a frozen dataclass, so a ``match``
statement destructures them positionally and instances compare by value.
Update and removal events carry a snapshot taken as the change was
applied - a listener that defers processing still sees the state the
event was about, and reads current state from ``AmpioClient.objects`` /
``modules`` when it wants that instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import AmpioModule, AmpioObject


@dataclass(frozen=True, slots=True)
class ObjectUpdated:
    """An object's state or metadata changed.

    Fires on live pushes, raw-channel edges, snapshot corrections, and
    catalogue rows that actually changed something - a re-requested
    catalogue that says nothing new dispatches nothing.
    """

    object: AmpioObject


@dataclass(frozen=True, slots=True)
class ObjectRemoved:
    """The account's authoritative catalogue stopped listing an object.

    Carries the final state; by dispatch time the id is gone from
    :pyattr:`AmpioClient.objects`. What triggers it differs by tier,
    because deletion differs by tool (see docs/identity.md). An app-side
    object delete soft-deletes: the
    ``config`` row stays with the hidden bit set - the admin tier sees an
    ``ObjectUpdated`` turning ``hidden``, never an eviction - while the
    app-sync surfaces drop the row, so the restricted tier evicts at its
    next catalogue reply, as it does on a grant revocation. A Designer
    save rebuilds the configuration (restarting the M-SERV along the
    way), and objects it deleted vanish from the ``config`` catalogue -
    the admin-tier eviction. Either way this is the signal to drop
    whatever entity was built on the object.
    """

    object: AmpioObject


@dataclass(frozen=True, slots=True)
class ModuleUpdated:
    """A module's catalogue row or its own diagnostics broadcast changed it.

    Fires for a module the list adds or changes and for each diagnostics
    broadcast. Both sources are administrator-only, so it never fires on a
    standard account.
    """

    module: AmpioModule


@dataclass(frozen=True, slots=True)
class ModuleRemoved:
    """The module list stopped listing a module.

    Carries the final state, after the store has dropped it. The module
    list is administrator-only, so this never fires on a standard account.
    """

    module: AmpioModule


@dataclass(frozen=True, slots=True)
class BusEvent:
    """A logical bus event (1-65535) raised by Ampio logic.

    Receiving these rides the administrator-only raw tree, so they never
    fire on a standard account - though such an account can raise events
    itself via :meth:`AmpioClient.send_event`.
    """

    number: int
    # Effective bus mac of whatever raised it: a module for a panel press,
    # the M-SERV itself for an event injected through the command surface.
    mac: int


@dataclass(frozen=True, slots=True)
class AvailabilityChanged:
    """The broker connection came up or went down.

    Fires for every transition the consumer did not cause itself: the
    connection coming up, an outage, and the drop preceding the terminal
    :class:`AuthFailed` / :class:`ConnectionDied` events (which are
    dispatched after it, so entities read unavailable by then). A
    consumer-initiated ``stop()`` is deliberately not reported - it is not
    news to the consumer, and reporting it made every orderly shutdown
    look like a lost connection. ``AmpioClient.available`` still reads
    False after a stop.
    """

    available: bool


@dataclass(frozen=True, slots=True)
class AuthFailed:
    """Terminal: the broker rejected the credentials after ``start()``.

    Carries the broker's reason string. The shape a credential change on
    the broker produces: by dispatch time ``AvailabilityChanged(False)``
    has fired and the connection loop has stopped for good, so this is the
    signal to drive a reauthentication flow. A rejection during
    ``start()`` itself raises ``AmpioAuthError`` there instead and
    dispatches nothing. The reason is also queryable as
    :pyattr:`AmpioClient.auth_failure`.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class ConnectionDied:
    """Terminal: the connection loop crashed and will not retry.

    The shape a bug in the connection loop itself produces - anything the
    loop does not recognize as a transport or credential failure. A bug
    triggered by one message's processing is not this: the client guards
    per message, dropping the failing payload with a logged traceback
    while the connection stays up. Dispatched after
    ``AvailabilityChanged(False)``, with the traceback logged and the
    reason kept in ``ConnectionStats.last_error``; without it a dead loop
    is indistinguishable from an outage the client is still retrying.
    Only a fresh ``start()`` recovers. A crash during ``start()`` itself
    makes ``start()`` raise ``AmpioConnectionError`` instead and
    dispatches nothing, mirroring the auth path.
    """

    reason: str


# The store's subset: what one inbound MQTT message can produce.
StoreEvent = ObjectUpdated | ObjectRemoved | ModuleUpdated | ModuleRemoved | BusEvent

# Everything a subscriber can receive.
ClientEvent = StoreEvent | AvailabilityChanged | AuthFailed | ConnectionDied
