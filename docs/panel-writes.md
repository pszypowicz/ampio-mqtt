# Panel writes

This page continues [`protocol.md`](protocol.md) with the raw CAN output frame
for panel status LEDs, relays, and open-collector outputs, and the panel buzzer.

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
`AmpioObject.leaf_io_no`, one below the 1-based raw state channel. The topic is
admin-only like the rest of the `ampio/to` tree. A binary output echoes on
`state/o/<ch+1>` in ~30-50 ms and on its object topic in ~150 ms. An
open-collector output echoes on `state/a/<ch+1>` as a u8 value and never on its
object topic, on any write path. The library therefore bridges `a` for those
objects. `confirm=` resolves on either edge.

On the admin tier, a `przekaznik` on a CAN module rides this frame when its leaf
class has a proven function byte. The frame is addressed by the object's own
leaf alone (mac, 0-based channel, and class). A class outside that table, and a
leafless object, stay on `/api`. There is no module-type table to maintain. Two
more writes stay on `/api`: the M-SERV's own virtual outputs, and every
`pulse_ms` write. The virtual outputs live in the server's DB, not on the CAN
bus. The raw frame has no timed form, so a panel output cannot pulse, and
`confirm=` is what shows that. The standard tier always publishes the `/api`
form, which a panel output ignores.

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

`buzz()`, `buzz_pattern()`, and `buzz_stop()` publish these frames on the admin
tier, addressed by `AmpioModule.id`. Any catalogued module is a valid address,
and the M-DOT panels are the proven targets. The touch-press beep length and its
per-field mask live in the panel's flash parameters, a Designer write the
library does not make.
