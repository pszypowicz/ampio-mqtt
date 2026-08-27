# The event stream

One stream carries everything the library learns, in the order the library
produced it. Every event is a frozen dataclass that carries immutable model
instances. What an event announced stays true, no matter how late a listener
processes it. When you want current state, read `client.objects` and
`client.modules` instead. Listeners are consumer code. A listener that raises is
logged, and the other listeners still run. A message whose processing fails is
dropped, and the connection stays up.

The docstrings in [`src/ampio_mqtt/events.py`](../src/ampio_mqtt/events.py) are
the authoritative contract for each class. This page is the map.

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

`of` narrows the subscription and, for a single class, types the callback
parameter precisely. `object_id` narrows further, to one object's events, and
dispatches in O(1) of the count of such registrations. This shape fits a
consumer with one listener per object. It applies only to the classes that carry
`.object` (`ObjectUpdated`, its `ObjectAdded` subclass, and `ObjectRemoved`).
Any other combination raises `ValueError` at registration time.

## What arrives

| Event                 | Announces                                                                                                                                                                                                                                | Tiers      | Terminal |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- | -------- |
| `ObjectUpdated`       | An object's state or metadata changed (live push, raw edge, snapshot correction, changed catalogue row).                                                                                                                                 | both       | no       |
| `ObjectAdded`         | An object's first event: initial discovery, a later catalogue addition, or re-creation after eviction. It subclasses `ObjectUpdated`, so `of=ObjectUpdated` subscriptions receive it too. `of=ObjectAdded` narrows to appearances alone. | both       | no       |
| `ObjectRemoved`       | The account's authoritative catalogue stopped listing an object.                                                                                                                                                                         | both       | no       |
| `ModuleUpdated`       | A module's catalogue row changed, or its diagnostics broadcast arrived.                                                                                                                                                                  | admin only | no       |
| `ModuleRemoved`       | The module list stopped listing a module.                                                                                                                                                                                                | admin only | no       |
| `BusEvent`            | Ampio logic raised a bus event (1-65535).                                                                                                                                                                                                | admin only | no       |
| `AvailabilityChanged` | The broker connection came up or went down (never for a `stop()`).                                                                                                                                                                       | both       | no       |
| `AuthFailed`          | The broker rejected the credentials after `start()`. Reauthenticate.                                                                                                                                                                     | both       | yes      |
| `ConnectionDied`      | The connection loop crashed. Only a fresh `start()` recovers.                                                                                                                                                                            | both       | yes      |

`ObjectAdded` subclasses `ObjectUpdated`. A `match` statement that destructures
the stream must put its `case ObjectAdded():` arm before
`case ObjectUpdated():`. In the reverse order, every `ObjectAdded` matches the
`ObjectUpdated` arm first, and the `ObjectAdded` arm never runs.

"Admin only" reflects what the M-SERV serves each account tier - see
[`account-tiers.md`](account-tiers.md). A standard account can still _raise_ bus
events (`send_event`), but it never receives them.

## Ordering and the terminal events

The contract lives on the `ampio_mqtt.events` module docstring and the event
classes themselves. In short: removals follow the updates of the catalogue reply
that caused them. `AvailabilityChanged(False)` precedes a terminal `AuthFailed`
or `ConnectionDied`. After a terminal event, only a fresh `start()` continues,
and a genuinely changed password means a new client.

An evicted object that later reappears follows the same first-event rule as
initial discovery. The eviction dispatches `ObjectRemoved`. The catalogue reply
that re-creates the id dispatches `ObjectAdded`, not `ObjectUpdated`, because
the store held nothing for that id in between.

## Delivery context

Events dispatch synchronously on the loop that ran `start()`. Most arrive from
the connection task's message handling. An explicit call such as
`resolve_records()` dispatches from the caller's own task instead. Either way it
is the same loop, so a listener gets the same ordering guarantees on both paths.
