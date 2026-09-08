# Commands

This page continues [`protocol.md`](protocol.md) with the `/api` write surface.

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
and an unknown verb is silently ignored. Where the baseline install lacks the
hardware to exercise a verb, the row says so.

**Commands are grant-scoped.** The M-SERV drops a command for an object outside
the account's grant, with no effect and no reply (see
[`account-tiers.md`](account-tiers.md)). The account's namespace likewise
carries state only for granted objects, including ones it just commanded.

**Designer's read-only checkbox drops writes the same way.** An object with
`AmpioObject.read_only` set accepts no `/api` write on any tier, admin included.
The marker and its consumer contract are in [`identity.md`](identity.md).

**The state echo is the only confirmation.** The library's `confirm=` option on
`command()` and the typed wrappers arms a waiter before the publish. The waiter
resolves on the next `ObjectUpdated` for the object. That is the per-object echo
on both tiers, or the earlier raw edge on the admin tier. The raw edge's arrival
suppresses the per-object copy. The echo is an observation and nothing stronger.
A concurrent change from another source satisfies it. A timeout is how every
silent drop shows. The drops are an ignored verb, an out-of-grant object, a
read-only object, or a command that changed nothing and thus pushed nothing.
Latency bounds the timeout choice. Most verbs echo in under ~200 ms on the
per-object path, and `arm`/`disarm` take ~1 s, so `confirm=2.0` covers the
measured surface. Scene commands and `setEvent` fan out beyond a single object
and offer no per-object echo.

The `ampio/to/<mac>/...` CAN tree is the other write path, documented in Ampio's
own MQTT API note. It has per-channel `cmd` topics and a `raw` hex channel that
covers CCT, DALI, blind angles, and display text. It is **admin-only** - the
broker drops a standard account's publishes there. The library uses the `/api`
surface, which works on both tiers, except for the binary-output writes
described below.

| Verb                               | Args                        | Notes                                                                                                                                                                                                                                                                                                                                        |
| ---------------------------------- | --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `turnOn`                           | -                           | Full on (255). Flags answer it too. `rgbw` objects ignore it (no effect, no reply) - see `setColors`.                                                                                                                                                                                                                                        |
| `turnOff`                          | -                           | Off. Flags answer it too. `rgbw` objects ignore it (no effect, no reply) - turn those off with `setColors 0/0/0/0`.                                                                                                                                                                                                                          |
| `switch`                           | -                           | Inverts the current state. Flags answer it too. `rgbw` objects ignore it (no effect, no reply).                                                                                                                                                                                                                                              |
| `open`                             | -                           | Cover to 100.                                                                                                                                                                                                                                                                                                                                |
| `close`                            | -                           | Cover to 0.                                                                                                                                                                                                                                                                                                                                  |
| `stop`                             | -                           | Halts a cover on either axis. Mid-travel, the position stream freezes at the halt point, and the commanded target is never reached. A slat rotation caught mid-turn freezes at an intermediate angle the same way. During the pre-travel slat phase it also cancels the pending move. Stationary, it is a silent no-op. Exposed as `stop()`. |
| `setValue`                         | `<0-255>[/<time>]`          | `time` is in 10 ms units and **reverts** the object afterwards - a timed pulse, not a fade. `rgbw` objects ignore it, with or without `time` (no effect, no reply) - see `setColors`.                                                                                                                                                        |
| `setColors`                        | `<R>/<G>/<B>/<W>`           | Also accepts one packed int (`R \| G<<8 \| B<<16 \| W<<24`), which is what object state reports back. Absent from the spec enum - undocumented but real. A fifth argument (a `time` in the `setValue` style) makes the M-SERV drop the whole command (no effect, no reply).                                                                  |
| `setRollerPos`                     | `<position>/<lamella>`      | Percent each. `101` omits an axis (see the slat-drag note below), so one command moves either axis alone or both together.                                                                                                                                                                                                                   |
| `setColor`                         | 24-bit `R \| G<<8 \| B<<16` | Dead on the baseline install: in the spec enum, but it has no effect and no reply on an `rgbw` object. Use `setColors`.                                                                                                                                                                                                                      |
| `setColorW`                        | `<rgb24>/<white>`           | Dead on the baseline install, exactly as `setColor`. Use `setColors`.                                                                                                                                                                                                                                                                        |
| `setTemperature`                   | `<°C>`                      | Regulator (`reg`) setpoint, echoed as `setTemperature` in the reg state push (see Live state). Absent from the spec enum (Ampio's MQTT API note only), yet it works.                                                                                                                                                                         |
| `setHeatingMode`                   | mode letter                 | All four letters in `HEATING_MODES` (`A`, `S`, `M`, `H`) write and echo on the baseline install. Each letter echoes in the state push's `mode` within the confirm window. `ThermostatState.mode` carries the letter verbatim.                                                                                                                |
| `arm`, `disarm`                    | `<pin>`                     | Flip a `satel_alarm` object's armed state, with a ~1 s echo. The `satel_` types cover alarm integrations generally, a Jablotron behind an M-CON included. Absent from the spec enum, yet it works. The paired "alarmed" object also reads 1 while the panel is in its exit-delay `arming` phase - on its own it is not a siren indicator.    |
| `setVolume`, `setInput`, `setSeek` | radio module                | In the spec enum. Untestable here - no radio module.                                                                                                                                                                                                                                                                                         |
| `setText`                          | `<text>`                    | Sets the `desc` field of the object's state push (`state` unchanged), fanned out to every user namespace.                                                                                                                                                                                                                                    |
| `setVirtualTemp`                   | `<°C>`                      | Drives a virtual temperature channel: plain decimal, echoed as the object's state (`21.5`, and zero echoes `0.0`).                                                                                                                                                                                                                           |
| `setVirtualValue`                  | `<0-255>`                   | Drives a virtual sensor channel, echoed as state. It works from the standard tier on a granted object.                                                                                                                                                                                                                                       |
| `setFakeValue`                     | `<0-255>`                   | Undocumented alias of `setVirtualValue`: absent from the spec enum (the server changelog names it), it drives the virtual channel identically.                                                                                                                                                                                               |

**`rgbw` on/off is a consumer-side color replay.** The verb rows above mark
`rgbw` as a type that ignores `turnOn`, `turnOff`, `switch`, and `setValue`. The
Ampio app remembers the light's last color client-side. It re-sends that color
via `setColors` for "on", and sends `setColors 0` for "off". The M-SERV's Matter
bridge does the same server-side. A Matter On/Off from Home Assistant appears on
the bus as `setColors` with the bridge's remembered color (or `0`). The publish
goes to the **admin** account's `/api` topic. The bridge is an ordinary MQTT
client of this same surface, so its writes are observable and grant-equivalent
to admin. The bridge sends the packed form as a signed 32-bit int (negative
values), which the M-SERV accepts. State echoes report the unsigned form. A
consumer that wants "on" for an `rgbw` object must follow the same pattern.
Remember the last non-zero state value (the packed color, decoded as
`AmpioObject.rgbw`), and replay it with `setColors`.

**No command carries a fade time.** No verb on this surface ramps an output. The
`setValue` `time` argument reverts the object after the delay, which makes it a
timed pulse. A dimmable `led` output pulses the same way: the new value, then
the revert, with no intermediate value in the state stream. `setColors` accepts
no time argument, and a fifth argument makes the M-SERV drop the command. The
object catalogue carries a per-object `fadeTime` column. That column is
device-side configuration, and it applies to every change of the object rather
than to one command.

The M-SERV Matter bridge advertises a per-command transition on its dimmable
outputs, and it does not honor the value. With transition times of 0, 5, and 20
seconds, the bridge emits an identical CAN frame sequence. The output reaches
its new level in about 0.3 seconds, with no intermediate steps in the state
stream. The bridge also takes the slower route. It emits a ten-frame command
where an `/api` `setValue` emits one frame.

A consumer must therefore not offer a per-command transition on a light. Ramps
are available only as device-side `fadeTime` configuration.

**Flags answer the switch verbs. Physical inputs do not.** The switch family
reaches more than outputs. A `flaga` object answers `turnOn`, `turnOff`, and
`switch` over `/api`. This works on the admin tier and on the standard tier. A
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
census of its `device_api` record. One module carries a physical input at
channel 0 and an unrelated relay at channel 0.

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
