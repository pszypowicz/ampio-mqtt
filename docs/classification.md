# Object classification

The `devicesDetails` payload returns one row per logical object. The library
classifies each row into exactly one kind: a `SensorKind` (sensor-side
platforms), an `InputKind` (binary or boolean platforms), an `OutputKind`
(controllable platforms), or a `ThermostatKind` (the `reg` temperature
controllers, climate platform). `classify(typ, interpretacja)` returns it. The
key is the object type (the wire's `typ_komponentu`) plus `interpretacja` (a
refinement for analog inputs). A component type is a measurement, a boolean
input, something controllable, or a thermostat, and never two of these. The four
kinds are thus alternatives, not optional slots on the object.

The tables themselves live in
[`src/ampio_mqtt/classification.py`](../src/ampio_mqtt/classification.py) and
are not repeated here. `TYPE_PROFILES` is one row per known `typ_komponentu`:
its kind, its raw-bridge channel prefix, and its system flag.
`_LIN_WEJ_BY_INTERP` maps a `lin_wej` object's `interpretacja` to its
measurement. The `OutputKind` flags say which command verbs an output answers,
and `InputKind.switchable` says the same for an input. A type absent from
`TYPE_PROFILES` (or an `interpretacja` absent from the analog map) still
surfaces, as the generic value sensor or the `analog_<n>` fallback.

## Wire notes the tables cannot carry

- `reg` state is the running flag. The rich climate readback (measured and
  target temperature, mode, cooling) is `AmpioObject.thermostat`.
- `detekcja` and `symulacja` are system objects: the M-SERV lists them
  unconditionally, with an empty `leafId`. `symulacja`'s raw-channel prefix is
  not yet bridged.
- `wej` is the per-channel physical-input object the Designer creates for a
  wired button. Its per-object payload is 255 pressed / 0 released. Its
  `interpretacja` mirrors `funkcja` (the channel number), so it refines nothing.
  It is read-only: the M-SERV drops every write verb for it on both account
  tiers, so `switchable` is False.
- `flaga` is the one input that answers the `turnOn`/`turnOff`/`switch` family,
  over `/api` on both account tiers, so `switchable` is True. A consumer can
  model a writable flag as a switch. See [`protocol.md`](protocol.md).
- `roleta_lamelki` is what the Ampio app writes when a cover's type is set to
  "blinds - slats". The same cover reads back as `roleta_procenty` while it is
  set to "blinds - percentage". Only the slats variant reports a `lammel` angle
  in its state payload, surfaced as `AmpioObject.lammel`.
- `rgbw` is the one output that ignores the `turnOn`/`turnOff`/`switch` family.
  The replay pattern Ampio's own consumers use for on/off is in
  [`protocol.md`](protocol.md).
- `bit8`, `bit16`, `sbit16`, and `bit32` are the integer sensor slots an
  M-CON-485 lands a Modbus reading in. Designer names them `bit 8`, `bit 16`,
  `sbit 16[+/-]`, and `bit 32`. All four classify into the open
  `value_<interpretacja>` family. The kind carries no unit and no device class,
  because what a slot holds is the installer's choice. `AmpioObject.unit` and
  `AmpioObject.decimals` serve what Designer stores for the object (see below).
- Ampio's vocabulary also carries `rgb`, `rgbww`, `ledww`, `ac`, `radio`,
  `ip_radio`, and `satel_alarm` - types absent from `TYPE_PROFILES` that
  classify as the generic value sensor. `satel_alarm` is the armed/alarmed flag
  pair of an alarm integration (a Jablotron behind an M-CON, so the prefix is
  not Satel-specific).

## Units and display precision

The kind tables fix a unit for the known measurements only (`temp` and the
`lin_wej` map). The open `value_<n>` family carries none. Designer stores the
installer's choice per object, and the library serves it on two columns and two
derived properties.

- `AmpioObject.url` is Designer's "Unit" field, verbatim. The Dictionary
  dialog's "Add unit to description" writes the same column. Designer writes a
  single space for "without unit".
- `AmpioObject.format` is Designer's "String format" field, verbatim. It is a
  printf conversion, and the dropdown appends the unit after it (`%.1f V`). A
  hand-typed format can hold the unit while the "Unit" field stays empty.
- `AmpioObject.unit` is the text after the last conversion in `format`, else the
  stripped `url`, else None. Designer's editor states that the format overwrites
  the unit, so the format tail wins when the two disagree.
- `AmpioObject.decimals` is the explicit precision of a fixed-point conversion
  (`%.3f` reads 3). Every other conversion reads None.

Both properties read None on every kind but a sensor. An output has no
measurement to label, and the system objects carry a placeholder in the `url`
column. Both columns reach the restricted tier. `format` rides `data/devices`,
and `url` rides the unfiltered `data/params_devices` table.

The unit a kind fixes and the unit Designer stores are separate facts. On a
`lin_wej` air-quality object the kind says no unit, and Designer can say `IAQ`.
A consumer picks which one it shows.

Designer's "Divide by" checkbox lives in the Dictionary dialog, not on the main
form. It sets `params` bit 5 (`MAKE_SEMICOLON` in the Designer enum) and stores
the divider in the `max` column. The M-SERV applies the divider to the published
state. A slot that holds 37 with "Divide by" 100 arrives as
`"state": "0.370000"`. The library needs no scale logic of its own, and it reads
neither the bit nor the divider. The same mechanism explains the float noise on
linear inputs, which Designer creates with "Divide by" 10.

## Platform shapes

What each `OutputKind.key` maps to on the consumer side - guidance the code
deliberately does not encode:

| `OutputKind.key` | Platform shape                    |
| ---------------- | --------------------------------- |
| `relay`          | switch                            |
| `dimmer`         | light with brightness             |
| `rgbw`           | light with RGBW color             |
| `cover`          | cover, open/close/stop only       |
| `cover_position` | cover with position               |
| `cover_tilt`     | cover with position and slat tilt |

## The kind-key vocabulary

`SENSOR_KIND_KEYS`, `INPUT_KIND_KEYS`, `OUTPUT_KIND_KEYS`, and
`THERMOSTAT_KIND_KEYS` export every static `kind.key` the library can emit. They
derive from `TYPE_PROFILES` and the `lin_wej` map at import time, so they cannot
drift. Two key families embed `interpretacja` and stay open.
`SENSOR_KIND_KEY_PREFIXES` (`analog_`, `value_`) names them. A consumer maps
`kind.key` to its own entity descriptions. Its CI must assert that every
exported key is either mapped or deliberately excluded. Each open prefix counts
as one decision. Then a library upgrade that adds a kind fails a test instead of
a silent drop of entities. That silent drop is the failure mode of every prior
Ampio consumer, from the M-SERV's own Matter bridge (unmapped objects return
`undefined` and vanish) to the config-driven predecessors.

## What classification keys on (and what it ignores)

Classification uses exactly two wire fields:

- **`typ_komponentu`** - the object type, the primary discriminator.
- **`interpretacja`** - a refinement, used only for `lin_wej` analog inputs.

It does **not** use:

- **`opis_menu` (the object name)** - display only. A consumer uses it as the
  entity's friendly name, and it never affects the kind. A renamed channel does
  not change what it is.
- **`funkcja` (the channel index)** - the physical channel index within the
  module. It routes raw channel events to the object, and it never affects the
  kind. It is not part of the object identity key. Use `object_key` for identity
  (see [`identity.md`](identity.md)).

`typ_komponentu` must be the primary key. On an M-SENS the **temperature**
object and the **humidity** object both carry `interpretacja=1`. Only
`typ_komponentu` tells them apart (`temp` is a fixed temperature kind, `lin_wej`
with `interpretacja=1` is humidity). A key on `interpretacja` alone mislabels
temperature as humidity.

## Why classification is split from visibility

Classification answers "what kind of thing is this row". Visibility (see
[`identity.md`](identity.md)) answers "surface it or not". They compose:

```python
should_surface = obj.visible          # classify() always yields a kind
platform = obj.kind    # SensorKind | InputKind | OutputKind | ThermostatKind
```

A hidden row is still classifiable, because the type field is intact, but it
must not become an entity. The two checks stay separate so that a consumer can
use one without the other. For example, a diagnostics report wants the hidden
row classified, so it can show "hidden objects of type X".
