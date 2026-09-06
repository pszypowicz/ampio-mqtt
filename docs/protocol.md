# Protocol surface

The M-SERV speaks two parallel topic trees on the same MQTT broker:

- **DB tree** - `ampio/fromDB/<user>/...` and `ampio/control/<user>/...`.
  User-scoped. It carries the discovery RPC pattern: publish a keyword on one of
  the control surfaces, and the matching `fromDB` topic gets a JSON response.
  Per-object live state arrives on `.../ob/<id>/state`.
- **Raw tree** - `ampio/from/<MAC>/state/...`. Global, not user-scoped, and
  retained: the broker holds every channel's last value and replays it on each
  subscribe. The M-SERV serves it only to administrator accounts (the broker ACL
  returns nothing on it for standard accounts). It carries decoded CAN
  per-channel state, keyed by the module's effective bus MAC. The library uses
  it as a low-latency, self-resyncing input bridge - see
  [`raw-channel-bridge.md`](raw-channel-bridge.md).

All topic helpers live in
[`src/ampio_mqtt/_protocol.py`](../src/ampio_mqtt/_protocol.py). Treat the
constants there as the authoritative source. The table below is a quick
reference.

## Discovery (request / response)

Publish the keyword as the payload on the control surface. The broker publishes
the response on the matching `fromDB` topic. Most responses are retained, so a
fresh subscriber sees the last value immediately.

The whole `config` surface answers only for **administrator** accounts. Standard
accounts get silence there (no error, no reply, independent of the account's app
permissions). Everything on the `data`, `states`, and `info` surfaces answers
for every account.

| Keyword          | Control surface               | Response topic                              | Shape                                                                                                                                                                                                                                                                                                                                                              |
| ---------------- | ----------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `devicesDetails` | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/devicesDetails` | `{Status, List: [{id, id_urzadzenia, typ_komponentu, interpretacja, funkcja, leafId, opis_menu, type, stan_json, ...}]}` - `type` is the Matter device type tag (see [`identity.md`](identity.md)).                                                                                                                                                                |
| `devices`        | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/devices`        | `{List: [{id, mac, mac_global, typ_urzadzenia, nazwa_urzadzenia, wersja_softu, wersja_pcb, ...}]}`                                                                                                                                                                                                                                                                 |
| `locations`      | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/locations`      | `{List: [{id, opis_menu, opis_rozwiniety}]}` - Designer's "Lokalizacja" name table. The per-output pointer that resolves through it rides the `device_api` tree below (see [`identity.md`](identity.md)).                                                                                                                                                          |
| `devices`        | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/devices`          | `{List: [...]}` - app-sync object catalogue: the `devicesDetails` row shape minus `params`/`stan_json`, filtered to the account's app grants.                                                                                                                                                                                                                      |
| `params_devices` | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/params_devices`   | `{List: [{id, params, param1, czas, powiazane, url}]}` - per-object `params` bitfields for the **full** catalogue (not grant-filtered).                                                                                                                                                                                                                            |
| `groups`         | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/groups`           | `{List: [{id, id_rodzica, opis_menu}]}` - room tree.                                                                                                                                                                                                                                                                                                               |
| `group_devices`  | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/group_devices`    | `{List: [{id_grupy, id_obiektu}]}` - object-to-room join.                                                                                                                                                                                                                                                                                                          |
| `scenes`         | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/scenes`           | `{List: [{id, parentId, sceneName, active, Actions, Infos, Schedules}]}` - scene catalogue. `Actions` are wire command strings, `Infos` their structured form.                                                                                                                                                                                                     |
| (empty)          | `ampio/control/<user>/states` | `ampio/fromDB/<user>/data/states`           | `{List: [{id, stan_json}]}` - bulk snapshot of the account's object states.                                                                                                                                                                                                                                                                                        |
| (empty)          | `ampio/control/<user>/info`   | `ampio/fromDB/<user>/data/info`             | `{Results: {mac, userId, serverVersion, serverRevision, mqttVersion, local_ip, device_id, ...}}` - server self-report, retained in the account namespace. `userId` is the asking account's id (`-1` for the reserved `admin` login). `AmpioServerInfo.access_tier` surfaces it for config flows. A running client's tier is decided by its authenticated username. |

## Module CAN records (`device_api`)

A third topic pair sits next to the `config`/`data` request-response surfaces
and the raw tree. `device_api/to/list` with the payload `0` asks the M-SERV for
every module's CAN-resident record at once. The reply lands on
`device_api/from/list` as `{devices: [...]}`. Each device carries `macUser` (the
override), `macProd` (the factory id), `protocol`, `name` (base64), and
`descriptions`, base64 of the per-output entries behind both the Matter device
type tag and the Designer "Lokalizacja" location pointer. The frame layout, the
descType enum, and the join rule that resolves an object to its entry are in
[`identity.md`](identity.md). The tree is admin-only, exactly like the raw tree.
`AmpioClient.resolve_records()` drives this pair. A consumer never calls it
directly.

The per-module pair serves the same record for one module.
`device_api/to/<machex>/get_data` (empty payload) answers on
`device_api/from/<MACHEX>/info`. Both macs are the factory id, never the
override. A module with a Designer override stays silent on its override mac,
the M-SERV's own row included. The M-SERV serves those requests one module at a
time, at a mean gap of 0.75 seconds on the reference install. The list reply
carries the same blobs in one message, so the library reads the list.

Each account namespace also carries a retained
`ampio/fromDB/<user>/md5/<keyword>` topic per app-sync table (`devices`,
`params_devices`, `groups`, `group_devices`, `scenes`, `resources`, `icons`,
`logging`). Each holds the MD5 of the exact reply payload the account receives,
per-account for the grant-filtered tables. The Designer SPA uses these to skip
redundant refetches. The library does not: the hashes cover neither the `config`
catalogues nor `states`, so the requests worth saving have no hash.

## Commands (write)

One topic per account carries every write, as plain text:

```
ampio/control/<user>/api      /api/set/<object_id>/<verb>[/<arg>...]
```

The verb vocabulary is the M-SERV's own HTTP control API, re-exposed over MQTT.
The OpenAPI spec embedded in the M-SERV web app bundle
(`http://<host>/assets/index-*.js`) lists it, but the enum is advisory in both
directions. `setColor`/`setColorW` are listed yet ignored on the wire, while
`setColors` and `setFakeValue` work without a listing. There is no reply topic.
The object's normal state topic reports the result, typically within ~200 ms,
and an unknown verb is silently ignored. Every row below states observed
behavior on the baseline server. Where the reference install lacks the hardware
to exercise a verb, the row says so.

**Commands are grant-scoped.** The per-user grant bounds writes exactly as it
bounds reads. The M-SERV drops a command for an object outside the account's
grant, with no effect and no reply. The identical command from an administrator
succeeds. The check covered non-granted objects of multiple component types. The
most recent pass sent `setColors` to an rgbw and `setValue` to a dimmer from the
standard account. An admin session observed both objects stay silent. A
granted-object positive control from the same account confirmed that its command
path works. The account's namespace likewise carries state only for granted
objects, including ones it just commanded.

**Designer's read-only checkbox drops writes the same way.** An object with
`params` bit 6 set (`AmpioObject.read_only`) accepts no `/api` write on any
tier, admin included. The M-SERV emits no CAN frames for it, so the drop is
server-side and silent. Reads are unaffected. The marker and its consumer
contract are in [`identity.md`](identity.md).

**The state echo is the only confirmation.** The library's `confirm=` option on
`command()` and the typed wrappers arms a waiter before the publish. The waiter
resolves on the next `ObjectUpdated` for the object. That is the per-object echo
on both tiers, or the earlier raw edge on the admin tier. The raw edge's arrival
suppresses the per-object copy. The echo is an observation and nothing stronger.
A concurrent change from another source satisfies it. A timeout is how every
silent drop surfaces: an ignored verb, an out-of-grant object, a read-only
object, or a command that changed nothing and thus pushed nothing. Latency
bounds the timeout choice. Most verbs echo in under ~200 ms on the per-object
path, and `arm`/`disarm` take ~1 s, so `confirm=2.0` covers the measured
surface. Scene commands and `setEvent` fan out beyond a single object and offer
no per-object echo.

The `ampio/to/<mac>/...` CAN tree is the other write path (documented in Ampio's
own MQTT API note, with per-channel `cmd` topics and a `raw` hex channel that
covers CCT, DALI, blind angles, and display text). It is **admin-only** - the
broker drops a non-admin account's publishes there. The library uses the `/api`
surface, which works on both tiers, except for the binary-output writes
described below.

| Verb                               | Args                        | Notes                                                                                                                                                                                                                                                                                                                                              |
| ---------------------------------- | --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `turnOn`                           | -                           | Full on (255). Flags answer it too. `rgbw` objects ignore it (no effect, no reply) - see `setColors`.                                                                                                                                                                                                                                              |
| `turnOff`                          | -                           | Off. Flags answer it too. `rgbw` objects ignore it (no effect, no reply) - turn those off with `setColors 0/0/0/0`.                                                                                                                                                                                                                                |
| `switch`                           | -                           | Inverts the current state. Flags answer it too. `rgbw` objects ignore it (no effect, no reply).                                                                                                                                                                                                                                                    |
| `open`                             | -                           | Cover to 100.                                                                                                                                                                                                                                                                                                                                      |
| `close`                            | -                           | Cover to 0.                                                                                                                                                                                                                                                                                                                                        |
| `stop`                             | -                           | Halts a cover on either axis. Mid-travel, the position stream freezes at the halt point, and the commanded target is never reached. A slat rotation caught mid-turn freezes at an intermediate angle the same way. During the pre-travel slat phase it also cancels the pending move. Stationary, it is a silent no-op. Exposed as `stop()`.       |
| `setValue`                         | `<0-255>[/<time>]`          | `time` is in 10 ms units and **reverts** the object afterwards - a timed pulse, not a fade.                                                                                                                                                                                                                                                        |
| `setColors`                        | `<R>/<G>/<B>/<W>`           | Also accepts one packed int (`R \| G<<8 \| B<<16 \| W<<24`), which is what object state reports back. Absent from the spec enum - undocumented but real.                                                                                                                                                                                           |
| `setRollerPos`                     | `<position>/<lamella>`      | Percent each. `101` omits an axis (see the slat-drag note below), so one command moves either axis alone or both together.                                                                                                                                                                                                                         |
| `setColor`                         | 24-bit `R \| G<<8 \| B<<16` | Dead on the baseline server: in the spec enum, but a live send to an `rgbw` object had no effect and no reply. Use `setColors`.                                                                                                                                                                                                                    |
| `setColorW`                        | `<rgb24>/<white>`           | Dead on the baseline server, same observation as `setColor`. Use `setColors`.                                                                                                                                                                                                                                                                      |
| `setTemperature`                   | `<°C>`                      | Regulator (`reg`) setpoint, echoed as `setTemperature` in the reg state push (see Live state). Absent from the spec enum (Ampio's MQTT API note only), yet it works.                                                                                                                                                                               |
| `setHeatingMode`                   | mode letter                 | All four claimed letters `A,S,M,H` write and echo on the baseline server. A live round-trip on a virtual regulator drove `S -> A -> H -> M -> S`, each echoed in the state push's `mode` within the confirm window. (An earlier observation that `S` was silently ignored does not reproduce.) `ThermostatState.mode` carries the letter verbatim. |
| `arm`, `disarm`                    | `<pin>`                     | Flip a `satel_alarm` object's armed state, with a ~1 s echo. The `satel_` types cover alarm integrations generally (verified on a Jablotron behind an M-CON). Absent from the spec enum, yet it works. The paired "alarmed" object also reads 1 while the panel is in its exit-delay `arming` phase - on its own it is not a siren indicator.      |
| `setVolume`, `setInput`, `setSeek` | radio module                | In the spec enum. Untestable here - no radio module.                                                                                                                                                                                                                                                                                               |
| `setText`                          | `<text>`                    | Sets the `desc` field of the object's state push (`state` unchanged), fanned out to every user namespace.                                                                                                                                                                                                                                          |
| `setVirtualTemp`                   | `<°C>`                      | Drives a virtual temperature channel: plain decimal, echoed as the object's state (`21.5`, and zero echoes `0.0`).                                                                                                                                                                                                                                 |
| `setVirtualValue`                  | `<0-255>`                   | Drives a virtual sensor channel, echoed as state. It works from the standard tier on a granted object.                                                                                                                                                                                                                                             |
| `setFakeValue`                     | `<0-255>`                   | Undocumented alias of `setVirtualValue`: absent from the spec enum (the server changelog names it), it drives the virtual channel identically.                                                                                                                                                                                                     |

**`rgbw` on/off is a consumer-side color replay.** The switch-verb rows above
mark `rgbw` as a type that ignores `turnOn` / `turnOff` / `switch`. Live
observation shows how Ampio's own consumers handle that. The Ampio app remembers
the light's last color client-side. It re-sends that color via `setColors` for
"on", and sends `setColors 0` for "off". The M-SERV's Matter bridge does the
same server-side. A Matter On/Off from Home Assistant surfaces on the bus as
`setColors` with the bridge's remembered color (or `0`). The publish goes to the
**admin** account's `/api` topic. The bridge is an ordinary MQTT client of this
same surface, so its writes are observable and grant-equivalent to admin. The
bridge sends the packed form as a signed 32-bit int (negative values), which the
M-SERV accepts. State echoes report the unsigned form. A consumer that wants
"on" for an `rgbw` object must follow the same pattern. Remember the last
non-zero state value (the packed color, decoded as `AmpioObject.rgbw`), and
replay it with `setColors`.

**No command carries a fade time.** No verb on this surface ramps an output. The
`setValue` `time` argument reverts the object after the delay, which makes it a
timed pulse. `setColors` accepts no time argument. The object catalogue carries
a per-object `fadeTime` column. That column is device-side configuration, and it
applies to every change of the object rather than to one command.

The M-SERV Matter bridge advertises a per-command transition on its dimmable
outputs, and it does not honor the value. A live test drove one dimmable output
through the bridge three times, with transition times of 0, 5, and 20 seconds.
The bridge emitted an identical CAN frame sequence on all three runs. The output
reached its new level in about 0.3 seconds every time, with no intermediate
steps in the state stream. The bridge also takes the slower route. It emits a
ten-frame command where an `/api` `setValue` emits one frame.

A consumer must therefore not offer a per-command transition on a light. Ramps
are available only as device-side `fadeTime` configuration.

**Flags answer the switch verbs. Physical inputs do not.** The switch family
reaches more than outputs. A `flaga` object answers `turnOn`, `turnOff`, and
`switch` over `/api`. This works on the admin tier and on the restricted tier. A
consumer can therefore model a writable flag as a switch entity. The library
reports this as `InputKind.switchable`.

A `wej` object is a physical input, and the module scans that hardware itself.
The M-SERV drops all three switch verbs for a `wej`. There is no effect and no
reply, on either account tier. `setValue` behaves the same way. A consumer must
treat a `wej` as read-only.

Do not aim the raw output frame at a flag channel or at an input channel. The
frame drives the binary output that carries that channel number, which is a
different device on the same module. Each leaf class numbers its channels in its
own space. A module reports the size of each space in the `supportedFunctions`
census of its `device_api` record. One observed module carries a physical input
at channel 0 and an unrelated relay at channel 0.

Scenes are driven by their own payloads on the same topic. The payload addresses
the scene, not an object:

| Payload                | Effect                                                                                                      |
| ---------------------- | ----------------------------------------------------------------------------------------------------------- |
| `/api/run/scene/<id>`  | Applies the scene's actions.                                                                                |
| `/api/off/scene/<id>`  | Turns off the objects the scene drives.                                                                     |
| `/api/undo/scene/<id>` | Restores those objects to the state they held before the run - distinct from `off`, which drives them to 0. |

The M-SERV replays the scene's own actions, so a consumer never sends them
itself. Scene commands are grant-scoped like any other. A scene that touches
objects outside a standard account's grant does nothing.

A `roleta_lamelki` object carries its lamella angle in a `lammel` field next to
`state` in its state payload. No other type emits it, so its presence is a
second, runtime signal that an object has slats.

Covers stream intermediate positions in 5% steps during travel, so a consumer
sees the movement rather than one jump to the target.

A position move on a blind drags its slats along mechanically, and the `101`
sentinel only means "send no angle", not "hold the angle". The slats end
wherever the travel leaves them: closed (`lammel` 0) after a downward move, open
(100) after an upward one. To land on a chosen angle instead, pass an explicit
`lamella` in the same command.

## Panel outputs

The M-DOT touch panels expose one binary output per touch field - the status LED
beside it. These outputs are unreachable through every documented command form.
The `/api` verbs (`turnOn`, `setValue`, `switch`) and the per-channel
`ampio/to/<MAC>/o/<ch>/cmd` topic are all silently dropped for them, with a DB
object present or not. A relay module answers the identical commands. The
Designer SPA does not use `/api` for these leaves either. This is an Ampio
limitation: a standard account holds no surface that reaches a panel output at
all.

The write that works is the raw CAN frame the SPA itself sends, captured live
and replicated from a plain client:

```
ampio/to/<machex>/raw      <fn>f9<value:2><channel:2>     (ASCII hex)
```

The first byte is the function the Designer sends the leaf's class: `0x30` for a
binary output (leaf class 257, relays and panel LEDs) and `0x32` for an
open-collector output (class 67, the M-INOC). A module drops `0x30` on a
class-67 leaf, live-proven: the write returns, nothing moves, and no frame
follows on the bus. `0xF9` is the set-u8 command. `channel` is the 0-based
output index - `AmpioObject.leaf_io_no`, one below the 1-based raw state
channel. The topic is admin-only like the rest of the `ampio/to` tree. A binary
output echoes on `state/o/<ch+1>` in ~30-50 ms and on its object topic in ~150
ms. An open-collector output echoes on `state/a/<ch+1>` as a u8 value and never
on its object topic, on any write path, so the library bridges `a` for those
objects. `confirm=` resolves on either edge.

On the admin tier, a `przekaznik` on a CAN module rides this frame when its leaf
class has a proven function byte, addressed purely by its own leaf (mac, 0-based
channel, and class). A class outside that table, and a leafless object, stay on
`/api`. There is no module-type table to maintain. Two more writes stay on
`/api`: the M-SERV's own virtual outputs (they live in the server's DB, not on
the CAN bus) and every `pulse_ms` write. The raw frame has no timed form, so a
panel output cannot pulse, and `confirm=` is what surfaces that. The restricted
tier always publishes the `/api` form, which a panel output ignores.

A module condition bound to the LED overrides such writes eventually, not
preventively. A live write to a condition-bound LED took effect and was
re-asserted by the panel ~9 s later, with the bound source unchanged. Durable
external control thus needs an LED that Designer logic does not drive. Create
its app object in Designer - the same recipe as any other output object.

## Legacy CAN bridge surfaces

Two request/response endpoints predate the `fromDB` catalogues. The Node-RED
palette (`node-red-contrib-ampio`) is their public consumer. Both are
admin-gated like the rest of the `ampio/to` tree.

**Module discovery** - it still answers on the baseline server. Publish `1` to
`ampio/to/can/dev/list`. The reply arrives, not retained, on
`ampio/from/can/dev/list`:

```json
{"devices": [{"mac": "C0DE", "user_mac": "1", "typ": 10, "pcb": 7,
              "date_prod": ..., "protocol": 23, "soft_ver": ...,
              "name": "<base64>"}]}
```

Macs are uppercase hex strings, and `user_mac` is the override (the M-SERV
reports its factory mac with override `1`). `typ` is the same enumeration as
`typ_urzadzenia` - every code on the baseline install resolves in
`_devtypes.json`. Older firmware wrapped the list as `{"s": ..., "d": [...]}`
with per-module capability counts (`i`, `o`, `a`, `au`, `t`, `f`). Both are
gone. Its value: a module enumeration independent of the `fromDB` config
surface, usable as a resolver cross-check.

**Per-module descriptions** - the palette documents it as an empty publish to
`ampio/to/<MAC>/description`, answered on `ampio/from/<MAC>/description` with a
JSON object keyed `<descType>_<index>` (base64 names). The palette reads
descTypes 12, 13, 16, 17 for outputs, 6 for flags, and 21 for IR. It indexes
descTypes 11, 13, 15, 17 from 256. On the baseline server the surface is
**dead**: probed live with both mac cases, empty payload, and a wildcard reply
subscription - no reply, nothing retained. Read names through the `device_api`
record instead (see [`identity.md`](identity.md)). The palette's contract is
recorded here for older bridge firmware only.

## The Designer's own surfaces

The web Designer (served by the M-SERV, bundle at `/assets/index-*.js`) is an
ordinary MQTT client of the same broker, so everything it does is observable and
reproducible.

**Transport.** Plain MQTT over websocket via mqtt.js, one connection for
everything. Locally that is `ws://<host>:9001` (alongside TCP 1883). Through
Ampio's cloud it is
`wss://<device_id>-0.<cloud-domain>:6214/?_cloud_access_token=...`, plus a
support tunnel on `cloud3.ampio.com`. Connect options: MQTT 3.1.1,
`keepalive: 180`, `clean: true`, `reconnectPeriod: 2000`, no will, QoS 0
throughout. The publish wrapper drops messages while disconnected instead of a
queue.

**Surfaces.** The Designer never publishes to the `ampio/control/<user>/api`
command surface - the `/api/set` strings in the bundle are only the embedded
OpenAPI spec. It works on:

- Config reads and saves: `ampio/control/admin/config/...`, with replies on
  `ampio/fromDB/admin/config/#`. This includes the `save/leaves` table that maps
  every output leaf to command function 48 = `0x30` - the frame documented under
  Panel outputs.
- The `device_api` tree: `get_data`, `name_wr`, `descriptions_wr`,
  `firmware_wr`, `mac_user_wr`, `ow_search`, plus the broadcast helpers (`list`,
  `discover`, `version`, `alive`, `devices_log`).
- The JSON-RPC pair
  `rpc/v1/ctx/admin/{call,response}/com.ampio.mserv.rpc.mqtt.restricted` (and a
  `.system.restricted` twin). Its methods include `device_raw_api`,
  `config_get`/`config_set`/`config_reload`, `sf_get`, and `params_set`, with
  `devices_status` notifications.
- Raw CAN writes: `ampio/to/<machex>/raw` and `rawf`, hex-encoded frames. The
  live-control vocabulary observed so far: the generic output write
  `[0x30, 0xF9, value, channel]` and DALI set `[57, 0xF9, ch, val]`.
  MLED-capable panels add an MLED family `[54, 0xDF, 1|2|3, ...]`, and flash
  config transfer is `[dst, 0xFB|0xFC, blockLo, blockHi, ...]`. The Designer
  also sends raw CAN frames to `hw/out` (first byte the send-with-id opcode,
  then `0x80|len`, a 32-bit CAN id, and the data).
- Flag writes carry their own function, `0x16`. An `/api` flag write makes the
  M-SERV emit six `hw/out` frames to the module that owns the flag, parts 0 to 5
  of `[0x16, part, b, b]`. The parts reassemble to a header, a 32-bit flag mask,
  and one value byte (`FF` on, `00` off). The mask bit is the 0-based flag
  index, one below the 1-based raw `f` channel, the same rule outputs follow
  (see Panel outputs). A verbatim replay of those frames drives the flag, but
  only through `hw/out`, because `ampio/to/<machex>/raw` and `rawf` drop
  function `0x16` while they accept `0x30`. The replay is also slower than
  `/api`, with six publishes against one and a median state echo of 68 ms
  against 40 ms. The library therefore keeps `/api` for flags.
- Raw feeds: `fc` / `fcocb`, `ampio/from/+/raw`, and the same `ampio/from` state
  tree this library consumes.

## Bus events

Events are logical signals numbered 1-65535 that Ampio's own logic raises and
reacts to. A wall-panel press can raise one, and a scenario can be bound to one.
They are independent of objects - an event carries no state, and it drives
whatever logic the installer bound to it.

| Direction | Topic / payload                                  |
| --------- | ------------------------------------------------ |
| Raise     | `ampio/control/<user>/api` ← `/api/setEvent/<n>` |
| Receive   | `ampio/from/<MAC>/event` → `<n>`                 |

The MAC on a received event identifies what raised it. A panel press carries the
module's own address. An event injected through the command surface carries the
M-SERV's (`1` by default).

The two directions are gated differently:

- **Raising** works on both account tiers, and nothing bounds it. The Ampio app
  shows a per-user rights list per event, but that list is not enforced here. A
  standard account raised an event it had no right to, checked against a control
  event created without that right. Because the logic behind an event can drive
  anything, this is how an account reaches objects it cannot command directly.
- **Receiving** is administrator-only. It rides the raw tree. A standard account
  that holds the event's right still sees nothing - not on the raw tree, and not
  anywhere in its own namespace.

On the CAN side an event is frame type `0x2B` with a 16-bit little-endian
number, low byte first - `FE 2B BD 00` for 189 and `FE 2B BD BD` for 48573. A
legacy 8-bit event is simply one whose high byte is zero.

That layout invites a suspicion worth a rule-out. Does logic bound to an 8-bit
event also fire for a 16-bit event that shares one of its bytes? This was tested
against a module rule bound to event 189 (`0x00BD`). Neither `0xBDBD` nor
`0xBD00` moved it, 189 itself toggled reliably, and an unrelated event did
nothing. The match is on the full 16-bit value, at least on the M-DOT firmware
this ran against.

**The M-SERV raises event 254 from its own MAC whenever a client asks for a
discovery refresh**, so `connect()` normally produces one. It is not periodic. A
purely passive listener sees no events at all. A consumer that only cares about
panel presses must filter on the originating MAC, and must not treat every event
as user intent.

## Live state

| Topic                                    | Payload                  | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ampio/fromDB/<user>/ob/<id>/state`      | `{state, desc, on}`      | One per object. `state` is the value (string), `desc` is the M-SERV's pretty form, `on` is server-side ms epoch. Cover (`roleta*`) pushes carry a `block` field in place of `desc`. Regulator (`reg`) objects push a richer shape instead: `{state, cooling, mode, measureTemp, setTemperature, on}`, every field a string, surfaced as `AmpioObject.thermostat`. The library surfaces `state`, `lammel`, and the reg readback from these. |
| `ampio/from/<MAC>/state/f/<ch>`          | plain text (`"0"`/`"1"`) | Flag channel, bridged to the owning object.                                                                                                                                                                                                                                                                                                                                                                                                |
| `ampio/from/<MAC>/state/i/<ch>`          | plain text (`"0"`/`"1"`) | Digital input channel, bridged to the owning object.                                                                                                                                                                                                                                                                                                                                                                                       |
| `ampio/from/<MAC>/state/o/<ch>`          | plain text (`"0"`/`"1"`) | Binary output channel, bridged to the owning `przekaznik` object (a panel status LED, or a relay output).                                                                                                                                                                                                                                                                                                                                  |
| `ampio/from/<MAC>/state/{a,t,rgbw}/<ch>` | varies                   | NOT subscribed by the library - the per-object topic is sufficient for these prefixes.                                                                                                                                                                                                                                                                                                                                                     |

## Library helpers

Which `AmpioClient` method drives which surface is API documentation, and it
lives on the client docstrings. [`discovery-flow.md`](discovery-flow.md) maps
the automatic bring-up sequence against the on-demand fetches.
