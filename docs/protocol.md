# Protocol surface

The M-SERV speaks two parallel topic trees on the same MQTT broker:

- **DB tree** - `ampio/fromDB/<user>/...` and `ampio/control/<user>/...`.
  User-scoped. Carries the discovery RPC pattern: publish a keyword on
  one of the control surfaces, get a JSON response on the matching
  `fromDB` topic. Per-object live state arrives on `.../ob/<id>/state`.
- **Raw tree** - `ampio/from/<MAC>/state/...`. Global, not user-scoped,
  retained (the broker holds every channel's last value and replays it
  on each subscribe), and served only to administrator accounts (the
  broker ACL returns nothing on it for standard accounts). Carries
  decoded CAN per-channel state, keyed by the module's effective bus
  MAC. Used as a low-latency, self-resyncing input bridge - see
  [`raw-channel-bridge.md`](raw-channel-bridge.md).

All topic helpers live in
[`src/ampio_mqtt/endpoints.py`](../src/ampio_mqtt/endpoints.py). Treat
the constants there as the authoritative source; the table below is a
quick reference.

## Discovery (request / response)

Publish the keyword as the payload on the control surface. The broker
publishes the response on the corresponding `fromDB` topic. Most
responses are retained, so a fresh subscriber sees the last value
immediately.

The whole `config` surface answers only for **administrator** accounts;
standard accounts get silence there (no error, no reply, independent
of the account's app permissions). Everything on the
`data`, `states`, and `info` surfaces answers for every account.

| Keyword          | Control surface               | Response topic                              | Shape                                                                                                                                                                                                                                                          |
| ---------------- | ----------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `devicesDetails` | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/devicesDetails` | `{Status, List: [{id, id_urzadzenia, typ_komponentu, interpretacja, funkcja, leafId, opis_menu, stan_json, ...}]}`                                                                                                                                             |
| `devices`        | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/devices`        | `{List: [{id, mac, mac_global, typ_urzadzenia, nazwa_urzadzenia, wersja_softu, wersja_pcb, ...}]}`                                                                                                                                                             |
| `locations`      | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/locations`      | `{List: [{id, opis_menu}]}` - Designer's "Location" name table only (see [`untapped-surfaces.md`](untapped-surfaces.md) for the per-output pointer half).                                                                                                      |
| `devices`        | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/devices`          | `{List: [...]}` - app-sync object catalogue: the `devicesDetails` row shape minus `params`/`stan_json`, filtered to the account's app grants.                                                                                                                  |
| `params_devices` | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/params_devices`   | `{List: [{id, params, param1, czas, powiazane, url}]}` - per-object `params` bitfields for the **full** catalogue (not grant-filtered).                                                                                                                        |
| `groups`         | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/groups`           | `{List: [{id, id_rodzica, opis_menu}]}` - room tree.                                                                                                                                                                                                           |
| `group_devices`  | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/group_devices`    | `{List: [{id_grupy, id_obiektu}]}` - object-to-room join.                                                                                                                                                                                                      |
| `scenes`         | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/scenes`           | `{List: [{id, parentId, sceneName, active, Actions, Infos, Schedules}]}` - scene catalogue. `Actions` are wire command strings, `Infos` their structured form.                                                                                                 |
| (empty)          | `ampio/control/<user>/states` | `ampio/fromDB/<user>/data/states`           | `{List: [{id, stan_json}]}` - bulk snapshot of the account's object states.                                                                                                                                                                                    |
| (empty)          | `ampio/control/<user>/info`   | `ampio/fromDB/<user>/data/info`             | `{Results: {mac, userId, serverVersion, serverRevision, mqttVersion, local_ip, device_id, ...}}` - server self-report; retained in the account namespace. `userId` is the asking account's id (`-1` for the reserved `admin` login) and drives tier detection. |

## Commands (write)

One topic per account carries every write, as plain text:

```
ampio/control/<user>/api      /api/set/<object_id>/<verb>[/<arg>...]
```

The verb vocabulary is the M-SERV's own HTTP control API re-exposed over
MQTT; the OpenAPI spec embedded in the M-SERV web app bundle
(`http://<host>/assets/index-*.js`) lists it, but the enum is advisory
in both directions: `setColor`/`setColorW` are listed yet ignored on the
wire, while `setColors` and `setFakeValue` work without being listed.
There is no reply topic - the object's normal state topic reports the
result, typically within ~200 ms, and an unknown verb is silently
ignored. Every row below states observed behavior on the baseline
server; where the reference install lacks the hardware to exercise a
verb, the row says so.

**Commands are grant-scoped.** The per-user grant bounds writes exactly
as it bounds reads: a command for an object outside the account's grant
is dropped with no effect and no reply, while the identical command from
an administrator succeeds. Checked against non-granted objects of
multiple component types - most recently `setColors` on an rgbw and
`setValue` on a dimmer, sent from the standard account while an admin
session observed both objects stay silent, with a granted-object
positive control from the same account confirming its command path
works. The account's namespace likewise carries state only for granted
objects, including ones it just commanded.

The `ampio/to/<mac>/...` CAN tree is the other write path (documented in
Ampio's own MQTT API note, with per-channel `cmd` topics and a `raw` hex
channel covering CCT, DALI, blind angles, and display text). It is
**admin-only** - a non-admin account's publishes there are dropped - so
the library uses the `/api` surface,
which works on both tiers.

| Verb                               | Args                        | Notes                                                                                                                                                                                                                                                                                                                                                     |
| ---------------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `turnOn`                           | -                           | Full on (255). Ignored by `rgbw` objects (no effect, no reply) - see `setColors`.                                                                                                                                                                                                                                                                         |
| `turnOff`                          | -                           | Off. Ignored by `rgbw` objects (no effect, no reply) - turn those off with `setColors 0/0/0/0`.                                                                                                                                                                                                                                                           |
| `switch`                           | -                           | Inverts current state. Ignored by `rgbw` objects (no effect, no reply).                                                                                                                                                                                                                                                                                   |
| `open`                             | -                           | Cover to 100.                                                                                                                                                                                                                                                                                                                                             |
| `close`                            | -                           | Cover to 0.                                                                                                                                                                                                                                                                                                                                               |
| `stop`                             | -                           | Halts a cover on either axis: mid-travel the position stream freezes at the halt point and the commanded target is never reached; a slat rotation is caught mid-turn the same way (slats freeze at an intermediate angle); during the pre-travel slat phase it also cancels the pending move; stationary it is a silent no-op. Exposed as `stop_cover()`. |
| `setValue`                         | `<0-255>[/<time>]`          | `time` is in 10 ms units and **reverts** the object afterwards - a timed pulse, not a fade.                                                                                                                                                                                                                                                               |
| `setColors`                        | `<R>/<G>/<B>/<W>`           | Also accepts one packed int (`R \| G<<8 \| B<<16 \| W<<24`), which is what object state reports back. Absent from the spec enum - undocumented but real.                                                                                                                                                                                                  |
| `setRollerPos`                     | `<position>/<lamella>`      | Percent each; `101` omits an axis (see the slat-drag note below), so one command moves either axis alone or both together.                                                                                                                                                                                                                                |
| `setColor`                         | 24-bit `R \| G<<8 \| B<<16` | Dead on the baseline server: in the spec enum, but a live send to an `rgbw` object had no effect and no reply. Use `setColors`.                                                                                                                                                                                                                           |
| `setColorW`                        | `<rgb24>/<white>`           | Dead on the baseline server, same observation as `setColor`. Use `setColors`.                                                                                                                                                                                                                                                                             |
| `setTemperature`                   | `<°C>`                      | Regulator (`reg`) setpoint; echoed as `setTemperature` in the reg state push (see Live state). Absent from the spec enum (Ampio's MQTT API note only), yet works.                                                                                                                                                                                         |
| `setHeatingMode`                   | mode letter                 | `M` switched a regulator from Schedule to Manual (state push `mode` went `S` -> `M`); sending `S` back was silently ignored, so only `M` is mapped of the claimed `A,S,M,H`; #73 tracks pinning the full mode vocabulary.                                                                                                                                 |
| `arm`, `disarm`                    | `<pin>`                     | Flip a `satel_alarm` object's armed state, ~1 s echo; the `satel_` types cover alarm integrations generally (verified on a Jablotron behind an M-CON). Absent from the spec enum, yet works. The paired "alarmed" object also reads 1 while the panel is in its exit-delay `arming` phase - on its own it is not a siren indicator.                       |
| `setVolume`, `setInput`, `setSeek` | radio module                | In the spec enum. Untestable here - no radio module.                                                                                                                                                                                                                                                                                                      |
| `setText`                          | `<text>`                    | Sets the `desc` field of the object's state push (`state` unchanged), fanned out to every user namespace.                                                                                                                                                                                                                                                 |
| `setVirtualTemp`                   | `<°C>`                      | Drives a virtual temperature channel: plain decimal, echoed as the object's state (`21.5`; zero echoes `0.0`).                                                                                                                                                                                                                                            |
| `setVirtualValue`                  | `<0-255>`                   | Drives a virtual sensor channel, echoed as state. Works from the standard tier on a granted object.                                                                                                                                                                                                                                                       |
| `setFakeValue`                     | `<0-255>`                   | Undocumented alias of `setVirtualValue`: absent from the spec enum (the server changelog names it), drives the virtual channel identically.                                                                                                                                                                                                               |

**`rgbw` on/off is a consumer-side color replay.** The switch-verb rows
above mark `rgbw` as ignoring `turnOn` / `turnOff` / `switch`; live
observation shows how Ampio's own consumers handle that. The Ampio app
remembers the light's last color client-side and re-sends it via
`setColors` for "on", with `setColors 0` for "off". The M-SERV's Matter
bridge does the same server-side: a Matter On/Off from Home Assistant
surfaces on the bus as `setColors` carrying the bridge's remembered
color (or `0`), published to the **admin** account's `/api` topic - the
bridge is an ordinary MQTT client of this same surface, so its writes
are observable and grant-equivalent to admin. The bridge sends the
packed form as a signed 32-bit int (negative values), which the M-SERV
accepts; state echoes report the unsigned form. A consumer wanting "on"
for an `rgbw` object should follow the same pattern: remember the last
non-zero state value (it is the packed color, decoded for consumers as
`AmpioObject.rgbw`) and replay it with `setColors`.

Scenes are driven by their own payloads on the same topic, addressing the
scene rather than an object:

| Payload                | Effect                                                                                                      |
| ---------------------- | ----------------------------------------------------------------------------------------------------------- |
| `/api/run/scene/<id>`  | Applies the scene's actions.                                                                                |
| `/api/off/scene/<id>`  | Turns off the objects the scene drives.                                                                     |
| `/api/undo/scene/<id>` | Restores those objects to the state they held before the run - distinct from `off`, which drives them to 0. |

The M-SERV replays the scene's own actions, so a consumer never sends
them itself. Scene commands are grant-scoped like any other: a scene
touching objects outside a standard account's grant does nothing.

A `roleta_lamelki` object carries its lamella angle in a `lammel` field
alongside `state` in its state payload; no other type emits it, so its
presence is a second, runtime signal that an object has slats.

Covers stream intermediate positions in 5% steps while travelling, so a
consumer sees the movement rather than one jump to the target.

Moving a blind's position drags its slats along mechanically, and the
`101` sentinel only means "send no angle" - not "hold the angle". The
slats end wherever the travel leaves them: closed (`lammel` 0) after a
downward move, open (100) after an upward one. Pass an explicit
`lamella` in the same command to land on a chosen angle instead.

## Bus events

Events are logical signals numbered 1-65535 that Ampio's own logic raises
and reacts to: a wall-panel press can raise one, and a scenario can be
bound to one. They are independent of objects - an event carries no state
and drives whatever logic the installer bound to it.

| Direction | Topic / payload                                  |
| --------- | ------------------------------------------------ |
| Raise     | `ampio/control/<user>/api` ← `/api/setEvent/<n>` |
| Receive   | `ampio/from/<MAC>/event` → `<n>`                 |

The MAC on a received event identifies what raised it: the module's own
address for a panel press, the M-SERV's (`1` by default) for an event
injected through the command surface.

The two directions are gated differently:

- **Raising** works on both account tiers and is bounded by nothing.
  The Ampio app shows a per-user rights list per event, but that list is
  not enforced here: a standard account raised an event it had no right
  to, checked against a control event created without that right. Since
  the logic behind an event can drive anything, this is how an account
  reaches objects it cannot command directly.
- **Receiving** is administrator-only. It rides the raw tree, and a
  standard account holding the event's right still sees nothing - not on
  the raw tree and not anywhere in its own namespace.

On the CAN side an event is frame type `0x2B` carrying a 16-bit
little-endian number, low byte first - `FE 2B BD 00` for 189 and
`FE 2B BD BD` for 48573. A legacy 8-bit event is simply one whose high
byte is zero.

That layout invites a suspicion worth ruling out: that logic bound to an
8-bit event also fires for a 16-bit event sharing one of its bytes.
Tested against a module rule bound to event 189 (`0x00BD`), neither
`0xBDBD` nor `0xBD00` moved it, while 189 itself toggled reliably and an
unrelated event did nothing. Matching is on the full 16-bit value, at
least on the M-DOT firmware this was run against.

**The M-SERV raises event 254 from its own MAC whenever a client asks for
a discovery refresh**, so `start()` normally produces one. It is not
periodic; a purely passive listener sees no events at all. A consumer
that only cares about panel presses should filter on the originating MAC
rather than treat every event as user intent.

## Live state

| Topic                                      | Payload                  | Notes                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------------ | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ampio/fromDB/<user>/ob/<id>/state`        | `{state, desc, on}`      | One per object. `state` is the value (string), `desc` is the M-SERV's pretty form, `on` is server-side ms epoch. Cover (`roleta*`) pushes carry a `block` field in place of `desc`; regulator (`reg`) objects push a richer shape instead: `{state, cooling, mode, measureTemp, setTemperature, on}`. The library surfaces only `state` (plus `lammel`) from these. |
| `ampio/from/<MAC>/state/f/<ch>`            | plain text (`"0"`/`"1"`) | Flag input channel; bridged to the owning object.                                                                                                                                                                                                                                                                                                                   |
| `ampio/from/<MAC>/state/i/<ch>`            | plain text (`"0"`/`"1"`) | Digital input channel; bridged to the owning object.                                                                                                                                                                                                                                                                                                                |
| `ampio/from/<MAC>/state/{a,t,rgbw,o}/<ch>` | varies                   | NOT subscribed by the library - the per-object topic is sufficient for these prefixes.                                                                                                                                                                                                                                                                              |

## Library helpers

| Method                       | What it does                                                                                                                                                                                                                                                                                               |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AmpioClient.start()`        | Connects, subscribes, publishes the six auto-discovery requests (`devicesDetails` and `devices` on `config`, `devices` and `params_devices` on `data`, plus the empty-payload `states` and `info` surfaces), waits for whichever catalogue answers, returns. See [`discovery-flow.md`](discovery-flow.md). |
| `AmpioClient.refresh()`      | Re-publishes the initial-discovery requests for the account's tier; `start()` issues it on every (re)connect, and a consumer calls it to notice server-side catalogue changes without reconnecting.                                                                                                        |
| `AmpioClient.fetch_rooms()`  | One-shot: publishes `groups` + `group_devices`, joins both responses into `{object_id: room_name}`. On-demand because the consumer decides when room hints are needed.                                                                                                                                     |
| `AmpioClient.send_event()`   | Raises a bus event; `BusEvent` subscribers see the ones the bus raises (administrator-only).                                                                                                                                                                                                               |
| `AmpioClient.fetch_scenes()` | One-shot: publishes `scenes`, returns `list[AmpioScene]`. Drive them with `run_scene()` / `turn_scene_off()` / `undo_scene()`.                                                                                                                                                                             |
| `AmpioClient.last_payloads`  | `{endpoint_name: payload}` of the verbatim retained response per endpoint (`details`, `devices`, `states`, `info`, `data_devices`, `params_devices`, `groups`, `group_devices`, `scenes`). Intended for the HA integration's diagnostics blob.                                                             |
