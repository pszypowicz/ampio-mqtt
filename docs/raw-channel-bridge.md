# Raw-channel bridge

The M-SERV publishes the same data twice:

- On the **per-object topic** `ampio/fromDB/<user>/ob/<id>/state`: user-scoped,
  not retained. Initial values come from the bulk `states` snapshot. This is the
  well-formed JSON form (`{state, desc, on}`, `desc` optional) and the one the
  library's per-object dispatcher consumes.
- On the **raw tree**, at `ampio/from/<MAC>/state/<prefix>/<channel>`: global,
  NOT user-scoped, and **retained**. The broker holds every channel's last value
  (edges republish retained), so a subscriber receives the complete current
  input state at subscribe time. This is the decoded-CAN form: plain-text
  payloads (`"0"`, `"1"`, ...) keyed by the module's effective bus MAC and a
  per-prefix channel index.

The raw form arrives **first** for input changes (the M-SERV decodes CAN and
publishes the raw value before it re-encodes the per-object record). For an
input platform that wants minimum latency on a button-press or flag toggle, the
raw form is the right source.

Once an object produced a raw message, it is **raw-owned**
(`AmpioObject.raw_owned`). The store then ignores the slower per-object echo
whole, and the bulk `states` snapshot skips the object. Its resync is the
retained raw state tree itself. Every reconnect's subscribe re-delivers that
tree whole, and the index that persists across sessions routes it. The library
subscribes to the four state wildcards at QoS 0 for that reason. The broker
replays retained values into a QoS 1 subscription through a queue of 1000
messages per client. The `f` prefix alone holds more values than that on the
baseline install. A QoS 0 subscription takes no queue slot, so its replay is
complete. A raw edge lost on a socket drop returns with the next replay, because
every channel is retained. On the first connect the retained tables arrive
before the catalogues can build that index. Initial values thus come from the
snapshot, and raw ownership begins with the object's first raw message. An input
whose module publishes no raw state (the M-SERV's own virtual objects) never
becomes raw-owned. It lives on the per-object path with snapshot resync,
unchanged.

The M-SERV serves the raw tree only to **administrator** accounts. The broker
ACL delivers nothing on `ampio/from/#` to a standard account, retained or live,
and a grant of the object to that account changes nothing. On the standard tier
the bridge never fires, and inputs update through the per-object topic instead,
100-140 ms later. The measurement, and the case for an administrator account,
are in [`account-tiers.md`](account-tiers.md).

Authoritative sources:
[`src/ampio_mqtt/_protocol.py`](../src/ampio_mqtt/_protocol.py) holds
`RAW_INPUT_WILDCARDS`, `RAW_OUTPUT_WILDCARD`, `RAW_ANALOG_WILDCARD`,
`RAW_DIAGNOSTICS_WILDCARD`, and `RAW_EVENT_WILDCARD` - together the six raw-tree
subscriptions - plus the router.
[`src/ampio_mqtt/classification.py`](../src/ampio_mqtt/classification.py) holds
the `channel_prefix` field on the `TYPE_PROFILES` rows. The store's
`_apply_raw_channel` applies a routed edge.

## What the library subscribes to

```
ampio/from/+/state/f/+   # flags  ("flaga")                                      QoS 0
ampio/from/+/state/i/+   # digital inputs  ("detekcja", "wej")                   QoS 0
ampio/from/+/state/o/+   # binary outputs ("przekaznik")                         QoS 0
ampio/from/+/state/a/+   # analog outputs ("przekaznik" on an open-collector leaf) QoS 0
ampio/from/+/b/4F        # per-module diagnostics broadcast                      QoS 1
ampio/from/+/event       # bus events                                            QoS 1
```

The four state wildcards ask for QoS 0. The broker retains every channel, and a
QoS 1 replay of that many values overflows its queue (see above). The
diagnostics and event filters keep QoS 1, the acknowledged leg for a live push.

The channel wildcards are bridged to the owning `AmpioObject`, so listeners see
the same push as for any other update. The `o` prefix covers every `przekaznik`
on a binary-output leaf. The `a` prefix covers the ones on an open-collector
leaf (class 67). Those report a u8 there and never on their object topic. A
touch panel's per-field status LEDs have no other retained surface, and a
relay's outputs share the channel shape, so both gain the raw-first path. The
event wildcard feeds `BusEventRaised` subscribers - a different surface with its
own semantics, described in [`bus-events.md`](bus-events.md).

Only the `admin` login subscribes to the tree. The SUBACK enforcement is in
[`account-tiers.md`](account-tiers.md), and the `subscribe_failures` counter in
[`discovery-flow.md`](discovery-flow.md).

## Module diagnostics (`b/4F`)

Next to the per-channel `state/` topics, a module broadcasts frames on
`ampio/from/<MAC>/b/<type>`, keyed by the CAN frame type. Type `4F` is the
diagnostics frame:

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
Each live frame also refreshes the module's `last_seen`. Subscribe to
`ModuleUpdated` to know when a module updates. Modules without a temperature
sensor (relays, panels) report voltage only.

The broker retains the last frame of each sending module, so the fields are
present from the subscribe replay on every connect. The broadcasts then refresh
them. A replayed frame updates the values but not `last_seen`, because a replay
says nothing about whether the module is alive now. The same holds for a
replayed raw channel value.

### Which modules send the frame

Not every module sends the frame. The sender set on the baseline install, by
module type and firmware (`AmpioModule.wersja_softu`):

| Type code | Model     | Firmware | Modules | Sends `b/4F` |
| --------- | --------- | -------- | ------- | ------------ |
| 3         | M-ROL-4s  | 10401    | 4       | no           |
| 4         | M-REL-8s  | 11703    | 6       | yes          |
| 8         | M-DOT-4   | 11529    | 3       | yes          |
| 9         | M-DOT-18  | 11529    | 2       | yes          |
| 10        | M-SERV-s  | 11639    | 1       | no           |
| 11        | M-DOT-9   | 11529    | 6       | yes          |
| 12        | M-OC-4s   | 11701    | 2       | yes          |
| 14        | M-INOC-8s | 11705    | 3       | yes          |
| 24        | M-REL-2   | 11703    | 2       | yes          |
| 25        | M-CON-s   | 908      | 1       | no           |
| 25        | M-CON-s   | 7007     | 1       | yes          |
| 26        | M-INOC-4p | 11703    | 1       | yes          |
| 33        | M-DOT-2   | 11529    | 1       | yes          |
| 44        | M-SENS    | 63       | 6       | no           |

After more than 100 days of broker uptime, the retained store held no frame from
a module marked "no". Those modules sent none in that time. They are alive on
other topics: an M-SENS pushes a sensor value every few seconds, and the M-SERV
and the older M-CON-s push bus frames. The two M-CON-s modules differ in
firmware alone, and only the newer one sends the frame.

A module that sends no frame keeps `supply_voltage` and `temperature` at None.
Its `last_seen` moves on object traffic alone: a state push or a raw edge for
one of its objects. After a connect, an empty `last_seen` is expected on a
roller module until one of its covers moves. On the M-SERV it stays empty until
one of its own objects pushes. A diagnostics reader must not take that empty
value as a dead module. A module that sends the frame shows liveness through it
even with no objects of its own.

### Timing

A sending module puts a frame on the topic on a 10 s grid. The frame appears
only when the voltage byte or the temperature byte differs from the previous
frame. A 30-minute capture on the baseline install holds 2082 live frames. No
module repeated a payload, and every gap was a multiple of 10 s. A module with a
steady reading stays silent between changes. The longest gap per module ranged
from 50 s to 410 s. The first frame after a connect came between 1 s and 183 s.
`last_seen` on a sending module with no object traffic can therefore lag by
minutes.

## What the library does not bridge

| Prefix                           | Why excluded                                                                                                                                                                                                                            |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `a` (other than class-67 relays) | Subscribed, but indexed for `przekaznik` objects on an open-collector leaf alone. Every other analog channel already arrives on the per-object topic with full precision and the right state-class metadata, so it drops at the lookup. |
| `t` (temperature)                | Same reasoning - the per-object form is sufficient.                                                                                                                                                                                     |
| `rgbw` (RGBW output)             | Output side. Latency is not the win it is for inputs, and the per-object form carries the user-friendly desc.                                                                                                                           |
| `o` (non-przekaznik)             | Subscribed, but indexed for `przekaznik` objects alone (see above). Channels of other output classes drop at the lookup.                                                                                                                |
| `symulacja` raw prefix           | Not bridged. The wire prefix is unverified, and the object updates through the per-object topic (see [`untapped-surfaces.md`](untapped-surfaces.md)).                                                                                   |

## The full retained prefix inventory

Passive retained sweeps of `ampio/from/+/state/#` on the baseline install show
more prefixes than the bridge consumes. The full set, with the module classes
that publish each:

| Prefix           | Publishes on                 | Meaning                                                                                                                                                                    |
| ---------------- | ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `f`              | every module                 | Binary flags - bridged.                                                                                                                                                    |
| `i`              | most modules                 | Binary inputs - bridged.                                                                                                                                                   |
| `o`              | most modules                 | Binary outputs - bridged for `przekaznik` (see above).                                                                                                                     |
| `a`              | dimmers, OC, rollers, relays | Analog output/input channels - bridged for `przekaznik` on an open-collector leaf (class 67), whose object topic never echoes. The per-object form is preferred elsewhere. |
| `t`              | M-SENS                       | Temperature - the per-object form is preferred.                                                                                                                            |
| `rgbw`           | RGBW-capable modules         | Packed color - the per-object form is preferred.                                                                                                                           |
| `afu8`, `afi16`  | M-SERV, panels, M-INOC       | Analog flags, u8 and i16 - the `FLAG_ANALOG_U8` / `FLAG_ANALOG_I16` functions of the module's own census (`supportedFunctions` in its `device_api` record).                |
| `au16l`          | M-SENS only                  | 16-bit sensor channels (humidity, pressure, noise, illuminance, air quality).                                                                                              |
| `au32`           | alarm gateway (M-CON) only   | 32-bit channels of the gateway's alarm system (`bit32` objects).                                                                                                           |
| `bi`, `bo`       | alarm gateway (M-CON) only   | Binary inputs and outputs of the gateway's alarm system (zone table, 128 channels each on the baseline install).                                                           |
| `armed`, `alarm` | alarm gateway (M-CON) only   | Alarm partition states - the pair behind `satel_alarm` objects.                                                                                                            |
| `rs`             | M-SERV only                  | Heating-zone setpoint in °C (`ampio/from/1/state/rs/<zone>`), the raw mirror of the `reg` object's target.                                                                 |

Two companion claims from a third-party integration stay unverified: `rsdn/<n>`
(day/night setpoints) and `rm/<n>` (operating mode, 0=calendar 1=manual-day
2=manual-night 3=holidays 4=block). Both use `<prefix>/<n>/cmd` as their write
leaf. The baseline install retains neither prefix and has no M-RT hardware to
produce them. The mode names match known Ampio heating semantics, so the claims
stay plausible and unverified. The bridge scope above does not change. This
inventory exists so that classification work starts from the real set.

## Routing key

Raw tree topics carry the module's effective MAC, not the user namespace. The
dispatcher's lookup table is keyed on `(module.mac, prefix, channel)`,
precomputed from the catalogue rather than resolved per message, and rebuilt on
every catalogue apply. `mac` is the Designer override, which a replacement
module re-uses (see [`identity.md`](identity.md)).
