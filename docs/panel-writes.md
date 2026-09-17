# Panel writes

This page continues [`protocol.md`](protocol.md) with the raw CAN output frame
for panel status LEDs, relays, and open-collector outputs, the panel buzzer, the
touch field colours, the touch lock, and the module identify LED.

## Panel outputs

The M-DOT touch panels expose one binary output per touch field - the status LED
beside it. These outputs are unreachable through every documented command form.
The `/api` verbs (`turnOn`, `setValue`, `switch`) and the per-channel
`ampio/to/<MAC>/o/<ch>/cmd` topic are all silently dropped for them, with a DB
object present or not. A relay module answers the identical commands. The
Designer does not use `/api` for these leaves either. This is an Ampio
limitation: a standard account holds no surface that reaches a panel output at
all.

The write that works is the raw CAN frame the SPA itself sends, replayed from a
plain client:

```
ampio/to/<machex>/raw      <fn>f9<value:2><channel:2>     (ASCII hex)
```

The first byte is the function the Designer sends the leaf's class. It is `0x30`
for a binary output (leaf class 257, relays and panel LEDs) and `0x32` for an
open-collector output (class 67, the M-INOC). A module drops `0x30` on a
class-67 leaf: the write returns, nothing moves, and no frame follows on the
bus. `0xF9` is the set-u8 command. `channel` is the 0-based output index -
`AmpioObject.address.channel`, one below the 1-based raw state channel. The
topic is admin-only like the rest of the `ampio/to` tree. A binary output echoes
on `state/o/<ch+1>` in ~30-50 ms and on its object topic in ~150 ms. An
open-collector output echoes on `state/a/<ch+1>` as a u8 value and never on its
object topic, on any write path. The library therefore bridges `a` for those
objects. `confirm=` resolves on either edge.

On `AmpioAdminClient`, a `przekaznik` on a CAN module rides this frame when its
leaf class has a proven function byte. The frame is addressed by the object's
own leaf alone (mac, 0-based channel, and class). A class outside that table
stays on `/api`. There is no module-type table to maintain. Two more writes stay
on `/api`: the M-SERV's own virtual outputs, and every `pulse_ms` write. The
virtual outputs live in the server's DB, not on the CAN bus. The raw frame has
no timed form, so a panel output cannot pulse, and `confirm=` is what shows
that. `AmpioClient` always publishes the `/api` form, which a panel output
ignores.

A module condition bound to the LED overrides such writes eventually, not
preventively. A write to a condition-bound LED takes effect, and the panel
re-asserts the bound state ~9 s later, with the bound source unchanged. Durable
external control thus needs an LED that Designer logic does not drive. Create
its app object in Designer - the same recipe as any other output object.

## Panel buzzer

The M-DOT panels carry a piezo buzzer. The Designer exposes it as a write-only
leaf with no DB object, no `/api` verb, and no state topic. The panel confirms
nothing on the bus. Two raw frames drive it, proven with microphone recordings
on a M-DOT-9 (firmware 11529):

```
ampio/to/<machex>/raw   0c0703 70 <fn> <tone> <time>
ampio/to/<machex>/raw   0c0703 71 <fn> <delay:2> <tone1> 00 <time1:2> <tone2> 00 <time2:2> <cycles>
```

`0c0703` is the prefix the Designer's "test condition" button puts before an
action. `70` and `71` are the buzzer destination with the action function in the
low nibble: simple and sequence. `fn` is 0 for OFF and 1 for ON. Two-byte fields
are little-endian. Every time field counts 10 ms ticks. The simple form caps at
2.55 s and the sequence fields at 655.35 s. The Designer widget's "[x100ms]"
label is wrong.

`tone` 1 to 31 sets the period. The fundamental is 16576 Hz / (tone + 1). Tone 0
is a silent rest. Loudness follows the piezo resonance near 2.4 kHz, and no
amplitude control exists:

| Tone                  | Fundamental       | Loudness                      |
| --------------------- | ----------------- | ----------------------------- |
| 6                     | 2368 Hz           | loudest, the Designer default |
| 4, 20                 | 3315 Hz, 789 Hz   | about 10 dB below tone 6      |
| 8, 12, 16, 24, 28, 31 | 1842 Hz to 518 Hz | 16 to 22 dB below tone 6      |

Tone 1 (8288 Hz) is barely audible and is not in the table.

`cycles` 0 repeats the sequence until another frame replaces it. The speed bytes
had no audible effect and stay 0. Cycles 255 is unverified.

Stopping has three rules. A simple time of 0 latches the buzzer on, and the
simple OFF frame ends it. The simple OFF does not end a running sequence,
because the next step turns the buzzer back on. A new sequence replaces a
running one, so a one-cycle sequence of tone 0 for 10 ms silences any pattern
within 100 ms. `buzz_stop()` sends that silent sequence and then the simple OFF.
When OFF cut a long single-tone sequence short, the panel emitted a 150 ms blip
at the sequence's scheduled end.

`buzz()`, `buzz_pattern()`, and `buzz_stop()` are `AmpioAdminClient` methods,
addressed by `AmpioModule.id`. Any catalogued module is a valid address, and the
M-DOT panels are the proven targets. The touch-press beep length and its
per-field mask are stored settings, readable as
`AmpioAdminClient.panel_settings` and written only by the Designer.

## Panel colours

The M-DOT panels light each touch field's icon, and show a separate status
indicator beside it. Both colours are stored settings, read back as
`AmpioAdminClient.panel_settings` (see
[`description-records.md`](description-records.md)). Two raw frames override
them at runtime:

```
ampio/to/<machex>/raw   0c0703 50 01 <red> <green> <blue> <white> <mask>
ampio/to/<machex>/raw   0c0703 60 01 <red> <green> <blue> <mask>
```

`50` is the icon backlight and `60` the status indicator, each the destination
with its action function in the low nibble. `01` is the sub-function, the one
the vendor's own stored conditions carry. The backlight has a white channel and
the status indicator does not, which is the only difference between the two
payloads.

`mask` selects the touch fields, one bit per field, least significant first, so
field 1 is bit 0. A panel reads the width its own field count needs and ignores
any surplus. The library therefore always sends the full three bytes, which
covers the 24 fields a panel can report, and no panel write depends on a record
sweep. A field number above 24 is refused with `AmpioValueError`, because no
frame can carry it. The ceiling is public as `MAX_PANEL_FIELD`, so a consumer
can check a field number before it calls. A field the panel does not have is
accepted and the panel ignores it.

These frames write nothing to the module's configuration. The stored settings
stay untouched, so a panel restart returns the configured colours. Nothing on
the bus reports the current colour, so no readback exists.

The sub-function `02` also sets the resting colour, and neither code outranks
the other. The last frame wins in either order. What else separates the two
codes is not known, and it does not show in the resting colour.

## Touch lock

A panel can ignore every touch for a while. A locked field broadcasts nothing at
all, not even the press, so the module suppresses the touch before it reaches
the bus. The lock is write-only. Nothing reports whether a panel is locked, and
a locked panel is indistinguishable from an idle one.

```
ampio/to/<machex>/raw   0c0703 f0 2f <fn> <time:2>
```

The key lock destination is 303, above one byte, so it takes the escape form:
`0xf0` with the action function in the low nibble, then the destination's low
byte `0x2f`. `fn` is 1 to lock and 0 to release at once. `time` is little-endian
10 ms ticks.

**The lock always expires.** There is no indefinite form. A zero time is a lock
of zero length, not a latch, so the panel beeps and a touch works at once. The
16-bit field caps a single lock at 655.35 s, about 10 minutes 55 seconds, so
holding a panel locked means re-arming before the current lock runs out.

A person can also toggle the lock from the panel, with the touch field
combination the Designer assigns. That combination is invisible on the bus, and
a manual lock expires the same way.

## Module identify

The Designer's Devices tab has an "Identify device" button in the Location
column. It sends a two-byte frame to the module, and the module lights its CAN
LED steadily until the stop frame:

```
ampio/to/<machex>/raw   7e01     start
ampio/to/<machex>/raw   7e00     stop
```

The first byte is the identify function `0x7E`. The second byte is the flag. The
module holds identify until the stop frame. The Designer sends its own stop 30 s
after the start. That timer is client-side, and the library has none, so a
consumer sends `identify_stop()` itself. A hold longer than about a minute is
unverified.

What lights up depends on the module family. A DIN-rail module lights its CAN
LED steadily (red on the M-ROL-4s) and returns to its blink on the stop frame. A
M-DOT-9 panel lights the LED on its back and shows nothing on the front. Its
backlight does not cycle, and the Designer's own button behaves the same, so a
wall-mounted panel gives no visible sign of identify.

No readback exists. The module confirms nothing on any topic, so `identify()`
and `identify_stop()` take no `confirm=`. Both are `AmpioAdminClient` methods,
addressed by `AmpioModule.id`, and any catalogued module is a valid address.

## Cover roller lock

The roller lock is the only write on this tree that addresses a cover channel
rather than a module. `AmpioObject.block` reads it back.

```
ampio/to/<machex>/raw   0c07 <state> f0 05 <sub> <mask> 00 00 00 00
```

`state` is `03` to set the lock and `00` to release it. `f0 05` is the escape
form of the roller action's destination, which is 261. `sub` is `0a` to gate
opening and `09` to gate closing. `mask` is one bit per roller channel, lowest
channel first, and the mask is as many bytes as the channel count needs. The
last four bytes are the delay and the time the roller time function ends with.
Designer hides both inputs for these sub-functions, so a lock set this way never
expires.

The bits are additive and each release clears its own bit alone. A cover locked
in both directions reads `block` 3. Releasing the opening bit then leaves
`block` at 1, which is the closing bit alone.

### Only one module generation implements it

A module states its roller channel count in its capability map. That count sizes
the mask, and it also decides whether the write works at all.

| Module reports          | Ordinary roller moves | Roller lock |
| ----------------------- | --------------------- | ----------- |
| A roller channel count  | Runs                  | Runs        |
| No roller channel count | Runs                  | Dropped     |

The second row is a real module generation, not a fault. Those modules take the
same envelope, the same destination and the same mask for an ordinary move, and
they discard the three lock sub-functions in silence. So the capability count is
the gate, and it is also the number the mask needs.

#### The destination is not the reason

A cover on a module that drops the lock took every combination of these three
axes, which is twelve frames:

| Axis         | Values sent        |
| ------------ | ------------------ |
| Sub-function | 8, 9 and 10        |
| Destination  | `f0 05` and `00`   |
| Mask width   | 1 byte and 2 bytes |

`block` stayed at 0 after each frame, and the matching release frame changed
nothing either.

A lock frame also went to a cover on a module that advertises a count. That
cover moved `block` to 2, and the release frame returned it to 0. The topic, the
envelope and the mask are therefore all correct as this library builds them.

`00` is the destination that the dropping module's own stored rules carry, and
an ordinary move sub-function on that destination runs its motor. The frame
reaches the module. The lock sub-functions are absent from that firmware.

#### What a consumer reads

`AmpioAdminClient.lock_target(object_id)` resolves one cover's lock write. It
returns a `LockTarget`, or one of the four `LockRefusal` members:

| Result              | Meaning                                                                                                              |
| ------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `LockTarget`        | A lock write reaches this cover. Carries the module mac, the cover's channel, and the module's roller channel count. |
| `NOT_A_COVER`       | The object is not a cover, and its channel index can belong to a cover on the same module.                           |
| `NOT_SWEPT`         | No sweep has answered the module, so the answer is not known.                                                        |
| `NO_ROLLER_COUNT`   | The module advertises no roller channel count and drops the lock frame.                                              |
| `PAST_LAST_CHANNEL` | The cover's channel lies past the roller count the module advertises.                                                |

`block_opening()`, `unblock_opening()`, `block_closing()` and
`unblock_closing()` call `lock_target()` and raise on a refusal. `NOT_SWEPT`
raises `AmpioValueError`, because `resolve_records()` fixes it. The other three
raise `AmpioUnsupported`, because the install cannot do the write. A consumer
that builds a lock control calls `lock_target()` after the sweep, and leaves the
control out on a refusal.

### A stored rule beats a runtime write

A module re-applies a saved condition's action state on every evaluation. A rule
whose action gates a direction, and whose trigger is false, clears a lock
written this way within a few seconds. Read `block` back twice before you trust
it, because the read straight after the frame always agrees with the frame.
