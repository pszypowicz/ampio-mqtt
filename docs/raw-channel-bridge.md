# Raw-channel bridge

The M-SERV publishes the same data twice:

- On the **per-object topic** `ampio/fromDB/<user>/ob/<id>/state`,
  user-scoped, not retained - initial values come from the bulk
  `states` snapshot. This is the well-formed JSON form
  (`{state, desc, on}`, `desc` optional) and the one the library's
  per-object dispatcher consumes.
- On the **raw channel tree** `ampio/from/<MAC>/state/<prefix>/<channel>`,
  global, NOT user-scoped, and **retained**: the broker holds every
  channel's last value (edges republish retained), so a subscriber
  receives the complete current input state of the install the moment it
  subscribes. This is the decoded-CAN form: plain-text payloads
  (`"0"`, `"1"`, ...) keyed by the module's effective bus MAC and a
  per-prefix channel index.

The raw form arrives **first** for input changes (the M-SERV decodes
CAN and publishes the raw value before re-encoding the per-object
record). For an input platform that wants minimum latency on a
button-press or flag toggle, the raw form is the right source.

Once an object has produced a raw message it is **raw-owned**
(`AmpioObject.raw_proven`): the slower per-object echo is ignored
whole, and the bulk `states` snapshot skips the object - its resync is
the retained raw table itself, re-delivered at every reconnect's
subscribe and routed through the index that persists across sessions.
On the first connect the retained tables arrive before the catalogues
can build that index, so initial values come from the snapshot and raw
ownership begins with the object's first raw message. An input whose
module publishes no raw table (the M-SERV's own virtual objects) never
becomes raw-owned and lives on the per-object path with snapshot
resync, unchanged.

The raw tree is served only to **administrator** accounts: the broker
ACL delivers nothing on `ampio/from/#` to a standard account, retained
or live, and granting that account the object changes nothing. On the
standard tier the bridge never fires and inputs update through the
per-object topic instead, 100-140 ms later - measured in
[`account-tiers.md`](account-tiers.md), which is also where the case for
an administrator account is laid out.

Authoritative source:
[`src/ampio_mqtt/_protocol.py`](../src/ampio_mqtt/_protocol.py)
(`RAW_INPUT_WILDCARDS` for the two input filters, plus
`RAW_DIAGNOSTICS_WILDCARD` and `RAW_EVENT_WILDCARD` - together the four
raw-tree subscriptions),
[`src/ampio_mqtt/classification.py`](../src/ampio_mqtt/classification.py)
(the `channel_prefix` field on the `TYPE_PROFILES` rows), and the
router in [`src/ampio_mqtt/_protocol.py`](../src/ampio_mqtt/_protocol.py)
plus the store's `_apply_raw_channel`.

## What the library subscribes to

```
ampio/from/+/state/f/+   # flags  ("flaga")
ampio/from/+/state/i/+   # digital inputs  ("detekcja", "wej")
ampio/from/+/b/4F        # per-module diagnostics broadcast
ampio/from/+/event       # bus events
```

The two channel wildcards are bridged to the owning `AmpioObject` so
listeners see the same push as for any other update. The event
wildcard feeds `BusEvent` subscribers - a different surface with its
own semantics, described in [`protocol.md`](protocol.md).

The whole tree is administrator-only (the broker rejects the filters
for any other account in the SUBACK with reason code 128), and only
the `admin` login subscribes to it - a standard client never asks, so
its connect carries no rejections at all. A rejection the admin client
does receive lands in the diagnostics snapshot's `subscribe_failures`
and warns: with a tier-shaped subscribe set it can only mean a broken
broker or ACL.

## Module diagnostics (`b/4F`)

Alongside the per-channel `state/` topics, each module periodically
broadcasts a frame on `ampio/from/<MAC>/b/<type>`, keyed by the CAN frame
type. Type `4F` is the diagnostics frame:

```json
{ "d": [254, 79, 63, 142], "m": 51966 }
```

`d[0]` is `0xFE` (broadcast) and `d[1]` is `0x4F` (diagnostics). The two
payload bytes decode as:

| Byte   | Meaning                | Decoding                                         |
| ------ | ---------------------- | ------------------------------------------------ |
| `d[2]` | CAN bus supply voltage | `× 0.2` → V                                      |
| `d[3]` | Module temperature     | `− 100` → °C, `0` means the module has no sensor |

Landed on `AmpioModule.supply_voltage` and `AmpioModule.temperature`, and
each frame refreshes the module's `last_seen`, so a module with no objects
of its own still shows liveness. Subscribe to `ModuleUpdated` to be
told when a module updates.

The broadcasts are periodic rather than retained, so the fields fill in
over the first minute of a session rather than immediately, and modules
without a temperature sensor (relays, panels) report voltage only.

## What the library deliberately does NOT subscribe to

| Prefix                 | Why excluded                                                                                                                                                                                              |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `a` (analog input)     | Already arrives on the per-object topic with full precision and the right state-class metadata. The raw form would force a re-classify per push.                                                          |
| `t` (temperature)      | Same reasoning - per-object form is sufficient.                                                                                                                                                           |
| `rgbw` (RGBW output)   | Output side. Latency is not the win it is for inputs, and the per-object form carries the user-friendly desc.                                                                                             |
| `o` (other)            | Catch-all; payload shape varies by module.                                                                                                                                                                |
| `symulacja` raw prefix | Classified as an input but the wire prefix is not yet confirmed. The object still updates through the per-object topic; tracked as a forward-work item in [`untapped-surfaces.md`](untapped-surfaces.md). |

## Routing key

Raw-channel topics carry the module's effective MAC, not the user
namespace. The dispatcher's lookup table is keyed on
`(module.mac, prefix, channel)`, precomputed from the catalogue rather
than resolved per message, and rebuilt on every catalogue apply. This
is why `mac` (the Designer override) and not `mac_global` (the factory
id) is the right module key here: a replacement module re-uses the
override, so the routing keeps working across a hardware swap.
