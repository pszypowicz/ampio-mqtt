"""Typed events dispatched to :meth:`AmpioClient.subscribe` listeners.

One stream carries object and module news from the store, record sweeps,
bus events, and connection-state transitions, in the order they were
produced. Two cross-class orderings are guaranteed: a removal
follows the updates the same catalogue reply produced, and
``AvailabilityChanged(False)`` precedes a terminal ``AuthFailed`` /
``ConnectionDied``. A held retained value that the reply makes routable is
not one of those updates. It carries an earlier message's value, and it
lands after the removals of its batch, never for a removed id.
Every class is a frozen dataclass, so a ``match``
statement destructures them positionally and instances compare by value.
Update and removal events carry a snapshot taken as the change was
applied - a listener that defers processing still sees the state the
event was about, and reads current state from ``objects``, or ``modules``
on the admin client, when it wants that instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import AmpioModule, AmpioObject, RecordSweep


@dataclass(frozen=True, slots=True)
class ObjectUpdated:
    """An object's state or metadata changed.

    Fires on live pushes, raw-channel edges, snapshot corrections, and
    catalogue rows that actually changed something. A re-requested
    catalogue that says nothing new dispatches nothing, and a
    :meth:`AmpioAdminClient.resolve_records` pass changes no object field
    at all. A catalogue row establishing an id the store did not already
    hold dispatches the :class:`ObjectAdded` subclass instead.
    """

    object: AmpioObject


@dataclass(frozen=True, slots=True)
class ObjectAdded(ObjectUpdated):
    """An object appeared in the account's catalogue.

    The object's first event: dispatched when a catalogue reply
    establishes an id the store did not hold - initial discovery, a
    Designer addition surfacing on a later reply, and the re-creation
    after an eviction all qualify (#79). A subclass of
    :class:`ObjectUpdated`, so ``of=ObjectUpdated`` subscriptions
    receive additions too; ``of=ObjectAdded`` narrows to appearances
    alone.
    """


@dataclass(frozen=True, slots=True)
class ObjectRemoved:
    """The catalogue stopped listing an object, or the door stopped admitting it.

    The door stops admitting a row when its hidden bit is set or its leaf
    is cleared.

    Carries the final state; by dispatch time the id is gone from
    :pyattr:`AmpioClient.objects`. This is the signal to drop whatever
    entity was built on the object. The deletion wire mechanics are in
    docs/visibility.md.
    """

    object: AmpioObject


@dataclass(frozen=True, slots=True)
class NotConfigured:
    """The catalogue lists rows the library cannot admit.

    The payload :class:`~ampio_mqtt.AmpioNotConfigured` carries, as the
    door fills it: ``objects`` holds the ``(id, name)`` pairs of every
    visible object row without a leaf, and ``collisions`` the ``(mac,
    module ids)`` pairs of every override mac two or more module rows
    share.

    A reply that changes either set reports both, so an event with two
    empty sides means the door refuses nothing now. That is the signal to
    take down whatever the fault raised. A standard account is served no
    module list, so ``collisions`` is always empty there.

    Not terminal. The rows stay out of ``objects``/``modules`` until a
    later reply lists them addressably, which produces
    :class:`ObjectAdded` or :class:`ModuleUpdated`. At connect time a
    non-empty side raises from
    :meth:`AmpioClient.wait_for_initial_discovery`.
    """

    objects: tuple[tuple[int, str | None], ...] = ()
    collisions: tuple[tuple[int, tuple[int, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class ModuleUpdated:
    """A module's catalogue row changed, or its diagnostics broadcast arrived.

    Fires for a module the list adds or changes and for each diagnostics
    broadcast. Both sources are administrator-only, so it never fires on a
    standard account. A :meth:`AmpioAdminClient.resolve_records` pass
    changes no module field, and reports itself with
    :class:`RecordSweepCompleted`.
    """

    module: AmpioModule


@dataclass(frozen=True, slots=True)
class ModuleRemoved:
    """The module list stopped admitting a module.

    Its row left the list, or another row now shares its mac.

    Carries the final state, after the store has dropped it. The module
    list is administrator-only, so this never fires on a standard account.
    """

    module: AmpioModule


@dataclass(frozen=True, slots=True)
class RecordSweepCompleted:
    """One :meth:`AmpioAdminClient.resolve_records` pass finished.

    Carries the :class:`RecordSweep` the call returned. The datasets on
    the admin client changed with it, so a consumer that reads them
    refreshes on this event. Dispatched from the caller's task, once per
    pass, on the admin client alone.
    """

    sweep: RecordSweep


@dataclass(frozen=True, slots=True)
class BusEventRaised:
    """A logical bus event (1-65535) raised by Ampio logic.

    Receiving these rides the administrator-only raw tree, so they never
    fire on a standard account - though such an account can raise events
    itself via :meth:`AmpioClient.set_event`.
    """

    event_number: int
    # Effective bus mac of whatever raised it: a module for a panel press,
    # the M-SERV itself for an event injected through the command surface.
    mac: int


@dataclass(frozen=True, slots=True)
class AvailabilityChanged:
    """The broker connection came up or went down.

    Fires for every transition the consumer did not cause itself: the
    connection coming up, an outage, and the drop preceding the terminal
    :class:`AuthFailed` / :class:`ConnectionDied` events. A
    consumer-initiated ``disconnect()`` is not reported, though
    ``AmpioClient.available`` still reads False after it.
    """

    available: bool


@dataclass(frozen=True, slots=True)
class AuthFailed:
    """Terminal: the broker rejected the credentials after a session came up.

    Carries the broker's reason string. By dispatch time
    ``AvailabilityChanged(False)`` has fired and the connection loop has
    stopped for good, so this is the signal to drive a reauthentication
    flow. A rejection before the first session comes up raises
    ``AmpioAuthError`` from ``connect()`` instead and dispatches nothing.
    """

    reason: str


@dataclass(frozen=True, slots=True)
class ConnectionDied:
    """Terminal: the connection loop crashed and will not retry.

    The shape a bug in the connection loop itself produces - anything the
    loop does not recognize as a transport or credential failure. A bug
    triggered by one message's processing is not this: the client guards
    per message and the connection stays up. Dispatched after
    ``AvailabilityChanged(False)``, with the traceback logged and the
    exception text kept in the diagnostics snapshot's ``last_error``. Only
    a fresh ``connect()`` recovers. A crash before the first session comes
    up makes ``connect()`` raise ``AmpioConnectionError`` instead and
    dispatches nothing.
    """

    reason: str


# The store's subset: what one inbound MQTT message can produce.
StoreEvent = (
    ObjectAdded
    | ObjectUpdated
    | ObjectRemoved
    | NotConfigured
    | ModuleUpdated
    | ModuleRemoved
    | BusEventRaised
)

# Everything a subscriber can receive.
ClientEvent = (
    StoreEvent
    | RecordSweepCompleted
    | AvailabilityChanged
    | AuthFailed
    | ConnectionDied
)
