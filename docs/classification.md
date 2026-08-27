# Object classification

The `devicesDetails` payload returns one row per logical object. The
library classifies each row into exactly one kind - a `SensorKind`
(sensor-side platforms), an `InputKind` (binary/boolean platforms), an
`OutputKind` (controllable platforms), or a `ThermostatKind` (the `reg`
temperature controllers, climate platform). `classify(typ, interpretacja)`
returns it, keying on the object type (the wire's `typ_komponentu`)
and `interpretacja` (a refinement for analog inputs). A component type is
a measurement, a boolean input, something controllable, or a thermostat,
never two, so the four are alternatives rather than optional slots on
the object.

The tables themselves live in
[`src/ampio_mqtt/classification.py`](../src/ampio_mqtt/classification.py)
and are not repeated here: `TYPE_PROFILES` is one row per known
`typ_komponentu` (its kind, raw-bridge channel prefix, and system flag),
`_LIN_WEJ_BY_INTERP` maps a `lin_wej` object's `interpretacja` to its
measurement, and the `OutputKind` flags say which command verbs an
output answers. A type absent from `TYPE_PROFILES` (and an
`interpretacja` absent from the analog map) still surfaces, as the
generic value sensor / `analog_<n>` fallback.

## Wire notes the tables cannot carry

- `reg` state is the running flag; the rich climate readback (measured
  and target temperature, mode, cooling) is `AmpioObject.thermostat`.
- `detekcja` and `symulacja` are system objects: always exposed by the
  M-SERV, visible even with an empty `leafId` (unless hidden).
  `symulacja`'s raw-channel prefix is not yet bridged.
- `wej` is the per-channel physical-input object the Designer creates
  for a wired button. Its per-object payload is 255 pressed / 0
  released, and its `interpretacja` mirrors `funkcja` (the channel
  number), so it refines nothing.
- `roleta_lamelki` is what the Ampio app writes when a cover's type is
  set to "blinds - slats"; the same cover reads back as
  `roleta_procenty` while it is set to "blinds - percentage". Only the
  slats variant reports a `lammel` angle in its state payload, surfaced
  as `AmpioObject.tilt_position`.
- `rgbw` is the one output that ignores the `turnOn`/`turnOff`/`switch`
  family; the replay pattern Ampio's own consumers use for on/off is in
  [`protocol.md`](protocol.md).
- Ampio's vocabulary also carries `rgb`, `rgbww`, `ledww`, `ac`,
  `radio`, `ip_radio`, and `satel_alarm` - types absent from
  `TYPE_PROFILES` that classify as the generic value sensor.
  `satel_alarm` is the armed/alarmed flags of an alarm integration (a
  Jablotron behind an M-CON, so the prefix is not Satel-specific).

## Platform shapes

What each `OutputKind.key` maps to on the consumer side - guidance the
code deliberately does not encode:

| `OutputKind.key` | Platform shape                    |
| ---------------- | --------------------------------- |
| `relay`          | switch                            |
| `dimmer`         | light with brightness             |
| `rgbw`           | light with RGBW colour            |
| `cover`          | cover, open/close/stop only       |
| `cover_position` | cover with position               |
| `cover_tilt`     | cover with position and slat tilt |

## The kind-key vocabulary

`SENSOR_KIND_KEYS`, `INPUT_KIND_KEYS`, `OUTPUT_KIND_KEYS`, and
`THERMOSTAT_KIND_KEYS` export every static `kind.key` the library can
emit, derived from `TYPE_PROFILES` and the `lin_wej` map at import time
so they cannot drift. Two key families embed `interpretacja` and stay
open; `OPEN_SENSOR_KEY_PREFIXES` (`analog_`, `value_`) names them. A
consumer mapping `kind.key` to its own entity descriptions should assert
in its CI that every exported key is either mapped or deliberately
excluded (treating each open prefix as one decision), so a library
upgrade that adds a kind fails a test instead of silently dropping
entities - the failure mode every prior Ampio consumer exhibits, from
the M-SERV's own Matter bridge (unmapped objects return `undefined` and
vanish) to the config-driven predecessors.

## What classification keys on (and what it ignores)

Classification uses exactly two wire fields:

- **`typ_komponentu`** - the object type; the primary discriminator.
- **`interpretacja`** - a refinement, used only for `lin_wej` analog inputs.

It does **not** use:

- **`opis_menu` (the object name)** - display only. A consumer uses it as the
  entity's friendly name; it never affects the kind. Renaming a channel does not
  change what it is.
- **`funkcja` (the channel index)** - identity only. It is part of a consumer's
  stable per-object key; it never affects the kind.

`typ_komponentu` has to be the primary key: on an M-SENS the **temperature**
object and the **humidity** object both carry `interpretacja=1`, and are told
apart only by `typ_komponentu` (`temp` -> a fixed temperature kind; `lin_wej`
with `interpretacja=1` -> humidity). Keying on `interpretacja` alone would
mislabel temperature as humidity.

## Why classification is split from visibility

Classification answers "what kind of thing is this row". Visibility
(see [`identity.md`](identity.md)) answers "should the consumer surface
it at all". They compose:

```python
should_surface = obj.visible          # classify() always yields a kind
platform = obj.kind    # SensorKind | InputKind | OutputKind | ThermostatKind
```

A ghost row (`leaf_id == ""`, not a system object) is still classifiable

- the type field is intact - but it should not become an entity.
  Keeping the two checks separate means a future consumer can use one
  without the other (e.g. diagnostics wants the ghost classified so the
  report can show "ghosts of type X").
