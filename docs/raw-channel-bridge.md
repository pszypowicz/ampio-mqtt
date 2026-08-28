# Raw-channel bridge

The M-SERV publishes the same data twice:

- On the **per-object topic** `ampio/fromDB/<user>/ob/<id>/state`: user-scoped,
  not retained. Initial values come from the bulk `states` snapshot. This is the
  well-formed JSON form (`{state, desc, on}`, `desc` optional) and the one the
  library's per-object dispatcher consumes.
- On the **raw channel tree** `ampio/from/<MAC>/state/<prefix>/<channel>`:
  global, NOT user-scoped, and **retained**. The broker holds every channel's
  last value (edges republish retained), so a subscriber receives the complete
  current input state at subscribe time. This is the decoded-CAN form:
  plain-text payloads (`"0"`, `"1"`, ...) keyed by the module's effective bus
  MAC and a per-prefix channel index.

The raw form arrives **first** for input changes (the M-SERV decodes CAN and
publishes the raw value before it re-encodes the per-object record). For an
input platform that wants minimum latency on a button-press or flag toggle, the
raw form is the right source.

Once an object produced a raw message, it is **raw-owned**
(`AmpioObject.raw_owned`). The store then ignores the slower per-object echo
whole, and the bulk `states` snapshot skips the object. Its resync is the
retained raw table itself. Every reconnect's subscribe re-delivers that table,
and the index that persists across sessions routes it. On the first connect the
retained tables arrive before the catalogues can build that index. Initial
values thus come from the snapshot, and raw ownership begins with the object's
first raw message. An input whose module publishes no raw table (the M-SERV's
own virtual objects) never becomes raw-owned. It lives on the per-object path
with snapshot resync, unchanged.

The M-SERV serves the raw tree only to **administrator** accounts. The broker
ACL delivers nothing on `ampio/from/#` to a standard account, retained or live,
and a grant of the object to that account changes nothing. On the standard tier
the bridge never fires, and inputs update through the per-object topic instead,
100-140 ms later. The measurement, and the case for an administrator account,
are in [`account-tiers.md`](account-tiers.md).

Authoritative sources:
[`src/ampio_mqtt/_protocol.py`](../src/ampio_mqtt/_protocol.py) holds
`RAW_INPUT_WILDCARDS`, `RAW_OUTPUT_WILDCARD`, `RAW_DIAGNOSTICS_WILDCARD`, and
`RAW_EVENT_WILDCARD` - together the five raw-tree subscriptions - plus the
router.
[`src/ampio_mqtt/classification.py`](../src/ampio_mqtt/classification.py) holds
the `channel_prefix` field on the `TYPE_PROFILES` rows. The store's
`_apply_raw_channel` applies a routed edge.

## What the library subscribes to

```
ampio/from/+/state/f/+   # flags  ("flaga")
ampio/from/+/state/i/+   # digital inputs  ("detekcja", "wej")
ampio/from/+/state/o/+   # binary outputs ("przekaznik")
ampio/from/+/b/4F        # per-module diagnostics broadcast
ampio/from/+/event       # bus events
```

The channel wildcards are bridged to the owning `AmpioObject`, so listeners see
the same push as for any other update. The `o` prefix covers every `przekaznik`
uniformly. A touch panel's per-field status LEDs have no other retained surface,
and a relay's outputs share the channel shape, so both gain the raw-first path.
The event wildcard feeds `BusEventRaised` subscribers - a different surface with
its own semantics, described in [`protocol.md`](protocol.md).

The whole tree is administrator-only (the broker rejects the filters for any
other account in the SUBACK with reason code 128). Only the `admin` login
subscribes to it. A standard client never asks, so its connect carries no
rejections at all. A rejection the admin client does receive lands in the
diagnostics snapshot's `subscribe_failures` and warns. With a tier-shaped
subscribe set, it can only mean a broken broker or ACL.

## Module diagnostics (`b/4F`)

Next to the per-channel `state/` topics, each module periodically broadcasts a
frame on `ampio/from/<MAC>/b/<type>`, keyed by the CAN frame type. Type `4F` is
the diagnostics frame:

```json
{ "d": [254, 79, 63, 142], "m": 51966 }
```

`d[0]` is `0xFE` (broadcast) and `d[1]` is `0x4F` (diagnostics). The two payload
bytes decode as:

| Byte   | Meaning                | Decoding                                         |
| ------ | ---------------------- | ------------------------------------------------ |
| `d[2]` | CAN bus supply voltage | `× 0.2` → V                                      |
| `d[3]` | Module temperature     | `− 100` → °C, `0` means the module has no sensor |

The values land on `AmpioModule.supply_voltage` and `AmpioModule.temperature`.
Each frame also refreshes the module's `last_seen`, so a module with no objects
of its own still shows liveness. Subscribe to `ModuleUpdated` to know when a
module updates.

The broadcasts are periodic, not retained, so the fields fill in over the first
minute of a session. Modules without a temperature sensor (relays, panels)
report voltage only.

## What the library deliberately does NOT subscribe to

| Prefix                 | Why excluded                                                                                                                                                                                         |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `a` (analog input)     | It already arrives on the per-object topic with full precision and the right state-class metadata. The raw form forces a re-classify per push.                                                       |
| `t` (temperature)      | Same reasoning - the per-object form is sufficient.                                                                                                                                                  |
| `rgbw` (RGBW output)   | Output side. Latency is not the win it is for inputs, and the per-object form carries the user-friendly desc.                                                                                        |
| `o` (non-przekaznik)   | Subscribed, but indexed for `przekaznik` objects alone (see above). Channels of other output classes drop at the lookup.                                                                             |
| `symulacja` raw prefix | Classified as an input, but the wire prefix is not confirmed. The object still updates through the per-object topic. It is listed as forward work in [`untapped-surfaces.md`](untapped-surfaces.md). |

## The full retained prefix inventory

Passive retained sweeps of `ampio/from/+/state/#` on the baseline install show
more prefixes than the bridge consumes. The full observed set, with the module
classes that publish each:

| Prefix           | Publishes on                 | Meaning                                                                                                                                                   |
| ---------------- | ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `f`              | every module                 | Binary flags - bridged.                                                                                                                                   |
| `i`              | most modules                 | Binary inputs - bridged.                                                                                                                                  |
| `o`              | most modules                 | Binary outputs - bridged for `przekaznik` (see above).                                                                                                    |
| `a`              | dimmers, OC, rollers, relays | Analog output/input channels - the per-object form is preferred.                                                                                          |
| `t`              | M-SENS                       | Temperature - the per-object form is preferred.                                                                                                           |
| `rgbw`           | RGBW-capable modules         | Packed color - the per-object form is preferred.                                                                                                          |
| `afu8`, `afi16`  | M-SERV, panels, M-INOC       | Analog flags, u8 and i16 - the `FLAG_ANALOG_U8` / `FLAG_ANALOG_I16` functions of the module's own census (`supportedFunctions` in its `get_data` record). |
| `au16l`          | M-SENS only                  | 16-bit sensor channels (humidity, pressure, noise, illuminance, air quality).                                                                             |
| `au32`           | alarm bridge (M-CON) only    | 32-bit channels of the bridged alarm system (`bit32` objects).                                                                                            |
| `bi`, `bo`       | alarm bridge (M-CON) only    | The bridged alarm system's binary inputs and outputs (zone table, 128 channels each on the observed install).                                             |
| `armed`, `alarm` | alarm bridge (M-CON) only    | Alarm partition states - the pair behind `satel_alarm` objects.                                                                                           |
| `rs`             | M-SERV only                  | Heating-zone setpoint in °C (`ampio/from/1/state/rs/<zone>`), the raw mirror of the `reg` object's target.                                                |

Two companion claims from a third-party integration stay unverified: `rsdn/<n>`
(day/night setpoints) and `rm/<n>` (operating mode, 0=calendar 1=manual-day
2=manual-night 3=holidays 4=block), with `<prefix>/<n>/cmd` as their write leaf.
The observed install retains neither prefix and has no M-RT hardware to produce
them. The mode names match known Ampio heating semantics, so the claims stay
plausible and unproven. The bridge scope above does not change. This inventory
exists so that classification work starts from the real set.

## Routing key

Raw-channel topics carry the module's effective MAC, not the user namespace. The
dispatcher's lookup table is keyed on `(module.mac, prefix, channel)`,
precomputed from the catalogue rather than resolved per message, and rebuilt on
every catalogue apply. That is why `mac` (the Designer override), and not
`mac_global` (the factory id), is the right module key here. A replacement
module re-uses the override, so the routing survives a hardware swap.
