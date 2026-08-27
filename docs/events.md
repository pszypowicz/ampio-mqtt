# The event stream

One stream carries everything the library learns, in the order it was
produced. Every event is a frozen dataclass carrying immutable model
instances, so what an event announced stays true however late a
listener processes it; read current state from `client.objects` /
`client.modules` when that is what you want. Listeners are consumer
code: one that raises is logged and the rest still run, and a message
whose processing fails is dropped without touching the connection.

The docstrings in
[`src/ampio_mqtt/events.py`](../src/ampio_mqtt/events.py) are the
authoritative contract for each class; this page is the map.

## Subscribing

```python
from ampio_mqtt import ModuleRemoved, ObjectRemoved, ObjectUpdated

client.subscribe(on_any_event)                      # the whole stream
client.subscribe(on_object, of=ObjectUpdated)       # one class, typed callback
client.subscribe(on_gone, of=(ObjectRemoved, ModuleRemoved))
client.subscribe(on_135, of=ObjectUpdated, object_id=135)  # one object

unsubscribe = client.subscribe(on_object, of=ObjectUpdated)
unsubscribe()                                       # deregister
```

`of` narrows the subscription and, for a single class, types the
callback parameter precisely. `object_id` narrows further, to one
object's events, and dispatches in O(1) of the total count of such
registrations - the shape for a consumer with one listener per object.
It applies only to the classes that carry `.object`
(`ObjectUpdated`, and its `ObjectAdded` subclass, and `ObjectRemoved`);
any other combination raises `ValueError` at registration time.

## What arrives

| Event                 | Announces                                                                                                                                                                                                                             | Tiers      | Terminal |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- | -------- |
| `ObjectUpdated`       | An object's state or metadata changed (live push, raw edge, snapshot correction, changed catalogue row).                                                                                                                              | both       | no       |
| `ObjectAdded`         | An object's first event: initial discovery, a later catalogue addition, or re-creation after eviction. Subclasses `ObjectUpdated`, so `of=ObjectUpdated` subscriptions receive it too; `of=ObjectAdded` narrows to appearances alone. | both       | no       |
| `ObjectRemoved`       | The account's authoritative catalogue stopped listing an object.                                                                                                                                                                      | both       | no       |
| `ModuleUpdated`       | A module's catalogue row changed, or its diagnostics broadcast arrived.                                                                                                                                                               | admin only | no       |
| `ModuleRemoved`       | The module list stopped listing a module.                                                                                                                                                                                             | admin only | no       |
| `BusEvent`            | Ampio logic raised a bus event (1-65535).                                                                                                                                                                                             | admin only | no       |
| `AvailabilityChanged` | The broker connection came up or went down (never for a `stop()`).                                                                                                                                                                    | both       | no       |
| `AuthFailed`          | The broker rejected the credentials after `start()`; reauthenticate.                                                                                                                                                                  | both       | yes      |
| `ConnectionDied`      | The connection loop crashed; only a fresh `start()` recovers.                                                                                                                                                                         | both       | yes      |

Because `ObjectAdded` subclasses `ObjectUpdated`, a `match` statement
that destructures the stream needs its `case ObjectAdded():` arm before
`case ObjectUpdated():` - the reverse order matches every `ObjectAdded`
on the `ObjectUpdated` arm first and never reaches the `ObjectAdded`
one.

"Admin only" reflects what the M-SERV serves each account tier - see
[`account-tiers.md`](account-tiers.md). A standard account can still
_raise_ bus events (`send_event`); it never receives them.

## Ordering and the terminal events

The contract lives on the `ampio_mqtt.events` module docstring and the
event classes themselves. In one line: removals follow the updates of
the catalogue reply that caused them, and `AvailabilityChanged(False)`
precedes a terminal `AuthFailed` / `ConnectionDied`, after which only a
fresh `start()` continues (a genuinely changed password means a new
client).

An object that is evicted and later reappears follows the same
first-event rule as initial discovery: the eviction dispatches
`ObjectRemoved`, and the catalogue reply that re-creates the id
dispatches `ObjectAdded`, not `ObjectUpdated`, because the store held
nothing for that id in between.

## Delivery context

Events dispatch synchronously on the loop that ran `start()`. Most
arrive from the connection task's message handling; an explicit call
such as `resolve_locations()` dispatches from the caller's own task
instead. Either way it is the same loop, so a listener gets the same
ordering guarantees regardless of which path produced the event.
