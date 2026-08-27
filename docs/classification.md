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
measurement. The `OutputKind` flags say which command verbs an output answers. A
type absent from `TYPE_PROFILES` (or an `interpretacja` absent from the analog
map) still surfaces, as the generic value sensor or the `analog_<n>` fallback.

## Wire notes the tables cannot carry

- `reg` state is the running flag. The rich climate readback (measured and
  target temperature, mode, cooling) is `AmpioObject.thermostat`.
- `detekcja` and `symulacja` are system objects: the M-SERV always exposes them,
  visible even with an empty `leafId` (unless hidden). `symulacja`'s raw-channel
  prefix is not yet bridged.
- `wej` is the per-channel physical-input object the Designer creates for a
  wired button. Its per-object payload is 255 pressed / 0 released. Its
  `interpretacja` mirrors `funkcja` (the channel number), so it refines nothing.
- `roleta_lamelki` is what the Ampio app writes when a cover's type is set to
  "blinds - slats". The same cover reads back as `roleta_procenty` while it is
  set to "blinds - percentage". Only the slats variant reports a `lammel` angle
  in its state payload, surfaced as `AmpioObject.tilt_position`.
- `rgbw` is the one output that ignores the `turnOn`/`turnOff`/`switch` family.
  The replay pattern Ampio's own consumers use for on/off is in
  [`protocol.md`](protocol.md).
- Ampio's vocabulary also carries `rgb`, `rgbww`, `ledww`, `ac`, `radio`,
  `ip_radio`, and `satel_alarm` - types absent from `TYPE_PROFILES` that
  classify as the generic value sensor. `satel_alarm` is the armed/alarmed flag
  pair of an alarm integration (a Jablotron behind an M-CON, so the prefix is
  not Satel-specific).

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
`OPEN_SENSOR_KEY_PREFIXES` (`analog_`, `value_`) names them. A consumer maps
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
- **`funkcja` (the channel index)** - identity only. It is part of a consumer's
  stable per-object key, and it never affects the kind.

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

A ghost row (`leaf_id == ""`, not a system object) is still classifiable,
because the type field is intact - but it must not become an entity. The two
checks stay separate so that a future consumer can use one without the other.
For example, a diagnostics report wants the ghost classified, so it can show
"ghosts of type X".
