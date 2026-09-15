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
The marker and its consumer contract are in [`visibility.md`](visibility.md).

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
covers DALI, blind angles, and display text. It is **admin-only** - the broker
drops a standard account's publishes there. CCT does not need it: the `setWW`,
`setWWPower` and `setWWColdness` verbs all answer a standard account on `/api`.
The library uses the `/api` surface, which works on both tiers, except for the
binary-output writes described in [`panel-writes.md`](panel-writes.md).

| Verb                               | Method                                     | Args                        | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| ---------------------------------- | ------------------------------------------ | --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `turnOn`                           | `turn_on()`                                | -                           | Full on (255). Flags answer it too. `rgbw` and `ledww` objects ignore it (no effect, no reply) - see `setColors` and `setWWPower`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `turnOff`                          | `turn_off()`                               | -                           | Off. Flags answer it too. `rgbw` objects ignore it (no effect, no reply) - turn those off with `setColors 0/0/0/0`. A `ledww` ignores it too - turn those off with `setWWPower 0`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `switch`                           | `switch()`                                 | -                           | Inverts the current state. Flags answer it too. `rgbw` objects ignore it (no effect, no reply). A `ledww` answers it, and it is the one verb of this family that a `ledww` answers: the M-SERV writes `255 - power` and `256 - coldness`, so two calls return the light to where it started.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `open`                             | `open()`                                   | -                           | Cover to 100.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `close`                            | `close()`                                  | -                           | Cover to 0.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `stop`                             | `stop()`                                   | -                           | Halts a cover on either axis. Mid-travel, the position stream freezes at the halt point, and the commanded target is never reached. A slat rotation caught mid-turn freezes at an intermediate angle the same way. During the pre-travel slat phase it also cancels the pending move. Stationary, it is a silent no-op.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `setValue`                         | `set_value()`                              | `<0-255>[/<time>]`          | `time` is in 10 ms units and **reverts** the object afterwards - a timed pulse, not a fade. The revert reaches a `przekaznik`, a `flaga` and a `led`. The analog flags take the timed form, set the value and hold it, so the `time` argument is discarded. They also take a wider value: a `flaga_liniowa` holds 0 to 255 and a `flaga_liniowa16` holds -32768 to 32767. Both truncate to the field width rather than refuse, so a 300 lands as 44 on a u8 flag and a 32768 lands as -32768 on the signed one. `rgbw` objects ignore the verb with or without `time` (no effect, no reply) - see `setColors`. A cover ignores it the same way in both forms, so the position axis moves through `setRollerPos` alone. A `ledww` ignores the plain form, and the timed form is not dropped. It sets the power, writes 0 into the coldness axis and never reverts. |
| `setColors`                        | `set_colors()`                             | `<R>/<G>/<B>/<W>`           | Also accepts one packed int (`R \| G<<8 \| B<<16 \| W<<24`), which is what object state reports back. Absent from the spec enum - undocumented but real. A fifth argument (a `time` in the `setValue` style) makes the M-SERV drop the whole command (no effect, no reply).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `setWW`                            | `set_ww()`                                 | `<packed>`                  | A CCT (`ledww`) light's two axes in one 16-bit argument, `power \| coldness<<8`, each axis a byte. Absent from the spec enum and from the Designer bundle - the mobile app is the only vendor client that sends it. It answers a standard account.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `setWWPower`                       | `set_ww_power()`                           | `<0-255>`                   | The power axis of a CCT light alone. The color temperature holds, which makes `0` a usable off that keeps the temperature for the next turn-on. A second argument, in the `setValue` `time` style, is accepted rather than dropped. It overwrites the coldness axis with 143 or 144, and the light holds that. The written value follows neither the argument nor the prior reading, and it is unexplained. Send the one-argument form. Same tier and same absence from the spec enum as `setWW`.                                                                                                                                                                                                                                                                                                                                                                 |
| `setWWColdness`                    | `set_ww_coldness()`                        | `<0-255>`                   | The color-temperature axis of a CCT light alone. The power holds, so a temperature change needs no read of the current power first. A second argument, in the `setValue` `time` style, is dropped: no effect and no reply. This axis is thus safe where `setWWPower` is not. Same tier and same absence from the spec enum as `setWW`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `setRollerPos`                     | `set_roller_pos()`, `set_roller_lamella()` | `<position>/<lamella>`      | Percent each. `101` omits an axis (see the slat-drag note below), so one command moves either axis alone or both together. This is the only verb that moves a cover's position. `setValue` is dropped on every cover type, with and without a `time` argument.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `setColor`                         | none (dead verb)                           | 24-bit `R \| G<<8 \| B<<16` | Dead on the baseline install: in the spec enum, but it has no effect and no reply on an `rgbw` object. Use `setColors`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `setColorW`                        | none (dead verb)                           | `<rgb24>/<white>`           | Dead on the baseline install, exactly as `setColor`. Use `setColors`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `setTemperature`                   | `set_temperature()`                        | `<°C>`                      | Regulator (`reg`) setpoint, echoed as `setTemperature` in the reg state push (see Live state in [`protocol.md`](protocol.md)). Absent from the spec enum (Ampio's MQTT API note only), yet it works.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `setHeatingMode`                   | `set_heating_mode()`                       | mode letter                 | All four letters in `HEATING_MODES` (`A`, `S`, `M`, `H`) write and echo on the baseline install. Each letter echoes in the state push's `mode` within the confirm window. `ThermostatState.mode` carries the letter verbatim.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `arm`, `disarm`                    | `command()`                                | `<pin>`                     | Flip a `satel_alarm` object's armed state, with a ~1 s echo. The `satel_` types cover alarm integrations generally, a Jablotron behind an M-CON included. Absent from the spec enum, yet it works. The paired "alarmed" object also reads 1 while the panel is in its exit-delay `arming` phase - on its own it is not a siren indicator.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `setVolume`, `setInput`, `setSeek` | `command()`                                | radio module                | In the spec enum. Untestable here - no radio module.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `setText`                          | `command()`                                | `<text>`                    | Sets the `desc` field of the object's state push (`state` unchanged), fanned out to every user namespace.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `setVirtualTemp`                   | `command()`                                | `<°C>`                      | Drives a virtual temperature channel: plain decimal, echoed as the object's state (`21.5`, and zero echoes `0.0`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `setVirtualValue`                  | `command()`                                | `<0-255>`                   | Drives a virtual sensor channel, echoed as state. It works from the standard tier on a granted object.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `setFakeValue`                     | `command()`                                | `<0-255>`                   | Undocumented alias of `setVirtualValue`: absent from the spec enum (the server changelog names it), it drives the virtual channel identically.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

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

**A `ledww` light needs no such replay.** Its two axes are independent, so
`setWWPower 0` turns the light off and holds the color temperature. The next
`setWWPower` with a non-zero value brings the light back at the temperature it
had. `turn_off()` sends that verb for a CCT object, and `turn_on()` refuses: the
library has no value to pick, because the power the light had before is the
consumer's to remember. `set_ww()` writes both axes at once, and
`set_ww_coldness()` writes the temperature axis alone. The state value packs
both as `power | coldness<<8`, decoded as `AmpioObject.cct`. The `coldness` byte
is what the wire carries and not a temperature in kelvin.

Each axis has a verb of its own, so a consumer never reads one axis back to
write the other. Reach for `set_ww()` only when both axes must move together. A
read-modify-write can reassert a stale value, because the other axis can change
between the read and the write.

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
sees the movement rather than one jump to the target. See the Cover parameters
section of [`description-records.md`](description-records.md) for a cover's
stored travel time and other settings.

A position move on a blind drags its slats along mechanically, and the `101`
sentinel only means "send no angle", not "hold the angle". The slats end
wherever the travel leaves them: closed (`lammel` 0) after a downward move, open
(100) after an upward one. To land on a chosen angle instead, pass an explicit
`lamella` in the same command.

## Push a notification to the mobile app

The same command topic carries a notification for the install's mobile app. The
payload addresses no object:

```
ampio/control/<user>/api      /api/pushNotification/<message>
```

Both account tiers can send it. The M-SERV answers on no topic, so a sender
learns nothing about delivery.

The message needs no escaping. A space, a UTF-8 character and a literal `%20`
all arrive unchanged, because the payload is an MQTT string rather than an HTTP
request line. A 300 character message arrives whole.

**Do not put a `/` in the message.** The M-SERV reads a second path segment as a
user name, so it drops the slash and everything after it. `AmpioClient` and
`send_notification()` refuse such a message rather than truncate it.

Every registered user of the install receives the notification. The OpenAPI spec
lists a `/api/pushNotification/<message>/<user>` form for one named user. The
baseline install registers too few push users to tell a targeted send from a
broadcast, so the library exposes the broadcast form alone.

## A blocked cover refuses every command

A cover state payload carries a `block` field next to `state`. The module keeps
two lock bits per cover, and the field is their value:

| `block` | Meaning                     | Reads as                              |
| ------- | --------------------------- | ------------------------------------- |
| 0       | No lock                     | Neither property is true              |
| 1       | Closing is blocked          | `blocks_closing`                      |
| 2       | Opening is blocked          | `blocks_opening`                      |
| 3       | Both directions are blocked | `blocks_closing` and `blocks_opening` |

The bits are independent. A cover with `block` 2 refuses an opening command and
runs a closing one, and the reverse holds for `block` 1.

**A blocked direction refuses the `/api` verbs too.** The module drops the
command. There is no error and no reply, so a consumer that only watches `state`
sees a cover that agreed to move and then did not. `block` is the only way to
tell the two apart.

A blocked cover keeps reporting its position, and the position stays correct. A
consumer must mark the cover unavailable for the blocked direction rather than
hide it.

### The bits gate the slat axis as well

The lock gates a direction, and it covers both axes of that direction. A slat
turn toward open counts as opening, and a turn toward closed counts as closing.
Both axes ride the same `setRollerPos` frame, so one bit refuses both:

| `block` | Travel open | Travel closed | Slats open | Slats closed |
| ------- | ----------- | ------------- | ---------- | ------------ |
| 0       | Runs        | Runs          | Runs       | Runs         |
| 1       | Runs        | Dropped       | Runs       | Dropped      |
| 2       | Dropped     | Runs          | Dropped    | Runs         |
| 3       | Dropped     | Dropped       | Dropped    | Dropped      |

A consumer that disables one control per blocked direction must disable the slat
controls on the same bit. A cover with `block` 2 keeps a working close control
and a working slat-toward-closed control.

### Which Designer actions set the bits

Designer sets them from a logic rule, through three roller actions it names
"Disable movement", "Disable closing" and "Disable opening". A rule holds the
lock for as long as its trigger holds. A wind alarm or a fire alarm can
therefore leave a cover blocked for a long time.

No `/api` verb sets or clears the flag. The CAN write tree does, through the
same three actions, so `block_opening()`, `unblock_opening()`, `block_closing()`
and `unblock_closing()` write it on the administrator tier. Only one module
generation implements those actions, and the wire form and the gate are in the
"Cover roller lock" section of [`panel-writes.md`](panel-writes.md).

`AmpioObject.block_writable` says whether a lock write reaches one cover's
module. `True` means the four methods work on that cover, and `False` means they
raise. `None` means that no sweep covered the module yet, so the answer is not
known. A consumer that builds a lock control must read the field first and must
not treat `None` as `False`. The field needs `resolve_records()` to have run.

The same Designer menu offers eight more roller actions, and none of them
touches the lock. Two carry names that suggest an override. "Close permanently"
and "Open permanently" are ordinary moves that latch and run to the end of
travel. A lock refuses each one in its blocked direction exactly as it refuses
"Close/stop" and "Open/stop".
