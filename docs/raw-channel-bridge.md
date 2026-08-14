# Raw-channel bridge

The M-SERV publishes the same data twice:

- On the **per-object topic** `ampio/fromDB/<user>/ob/<id>/state`,
  user-scoped, not retained - initial values come from the bulk
  `states` snapshot. This is the well-formed JSON form
  (`{state, desc, on}`, `desc` optional) and the one the library's
  per-object dispatcher consumes.
- On the **raw channel tree** `ampio/from/<MAC>/state/<prefix>/<channel>`,
  global, NOT user-scoped. This is the decoded-CAN form: plain-text
  payloads (`"0"`, `"1"`, ...) keyed by the module's effective bus MAC
  and a per-prefix channel index.

The raw form arrives **first** for input changes (the M-SERV decodes
CAN and publishes the raw value before re-encoding the per-object
record). For an input platform that wants minimum latency on a
button-press or flag toggle, the raw form is the right source.

The raw tree is served only to **administrator** accounts: the broker
ACL delivers nothing on `ampio/from/#` to a standard account, retained
or live, and granting that account the object changes nothing. On the
standard tier the bridge never fires and inputs update through the
per-object topic instead, 100-140 ms later - measured in
[`account-tiers.md`](account-tiers.md), which is also where the case for
an administrator account is laid out.

Authoritative source:
[`src/ampio_mqtt/const.py`](../src/ampio_mqtt/const.py)
(`RAW_INPUT_WILDCARDS`, `_INPUT_CHANNEL_PREFIX`) and the dispatcher in
[`src/ampio_mqtt/_store.py`](../src/ampio_mqtt/_store.py)
(`_handle_raw_channel`).

## What the library subscribes to

```
ampio/from/+/state/f/+   # flags  ("flaga")
ampio/from/+/state/i/+   # digital inputs  ("detekcja")
ampio/from/+/b/4F        # per-module diagnostics broadcast
```

The two channel wildcards are bridged to the owning `AmpioObject` so
listeners see the same push as for any other update.

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
of its own still shows liveness. Register `add_module_listener()` to be
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
`(module.mac, prefix, channel)` and resolves to an `object_id` by
walking `client.objects` once at discovery time. This is why `mac`
(the Designer override) and not `mac_global` (the factory id) is the
right module key here: a replacement module re-uses the override, so
the routing table stays valid across a hardware swap without a
rebuild.
