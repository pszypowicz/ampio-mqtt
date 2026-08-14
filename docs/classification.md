# Object classification

The `devicesDetails` payload returns one row per logical object. The
library classifies each row into a `SensorKind` (sensor-side platforms)
and/or an `InputKind` (binary/boolean platforms), or leaves it
unclassified. A single `classify(typ_komponentu, interpretacja)` returns
both, keying on `typ_komponentu` (the object type) and `interpretacja`
(a refinement for analog inputs).

Authoritative source:
[`src/ampio_mqtt/const.py`](../src/ampio_mqtt/const.py)
(`classify`, the `TYPE_PROFILES` table, and `_LIN_WEJ_BY_INTERP`).

## `typ_komponentu` truth table

The Sensor / Input / System columns are one row each in the
`TYPE_PROFILES` table in `const.py` (a type's `sensor`/`analog`/`numeric`,
`input`, and `system` fields) - keep that table in sync when a new type
is added.

| `typ_komponentu`  | Sensor? | Input? | System? | Note                                                                                                                                       |
| ----------------- | ------- | ------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `temp`            | yes     | no     | no      | Temperature reading, °C.                                                                                                                   |
| `lin_wej`         | yes     | no     | no      | Analog input - kind set by `interpretacja` (see below).                                                                                    |
| `bit32`           | yes     | no     | no      | Generic 32-bit measurement (units unknown).                                                                                                |
| `flaga`           | no      | yes    | no      | Generic boolean flag (logic flag, button-press hold, etc.).                                                                                |
| `detekcja`        | no      | yes    | yes     | Motion-style detection. Always visible.                                                                                                    |
| `symulacja`       | no      | yes    | yes     | Presence simulation. Always visible. Raw-channel prefix not yet bridged.                                                                   |
| `przekaznik`      | no      | no     | no      | Relay output - future `switch` platform.                                                                                                   |
| `rgbw`, `led`     | no      | no     | no      | Color/light outputs - future `light` platform.                                                                                             |
| `roleta_procenty` | no      | no     | no      | Roller percentage - future `cover` platform.                                                                                               |
| (anything else)   | "value" | no     | no      | Generic value-only sensor with no state class - the fallback for objects whose metadata has not arrived or whose type is not in the table. |

## `lin_wej` interpretation table

For a `lin_wej` object the measurement is selected by `interpretacja`
(`_LIN_WEJ_BY_INTERP` in
[`src/ampio_mqtt/const.py`](../src/ampio_mqtt/const.py)):

| `interpretacja` | `SensorKind.key` | Unit  | HA device class        |
| --------------- | ---------------- | ----- | ---------------------- |
| 1               | `humidity`       | `%`   | `humidity`             |
| 2               | `pressure_abs`   | `hPa` | `atmospheric_pressure` |
| 3               | `loudness`       | `dB`  | `sound_pressure`       |
| 4               | `illuminance`    | `lx`  | `illuminance`          |
| 5               | `iaq`            | -     | `aqi`                  |
| 6               | `pressure_rel`   | `hPa` | `pressure`             |
| 7               | `co2`            | `ppm` | `carbon_dioxide`       |

Unknown values fall through to a generic `analog_<n>` SensorKind with no
device class, so a future M-SENS variant still surfaces as a sensor.

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

```
should_surface = classify(...) is not None and obj.visible
```

A ghost row (`leaf_id == ""`, not a system object) is still classifiable

- the type field is intact - but it should not become an entity.
  Keeping the two checks separate means a future consumer can use one
  without the other (e.g. diagnostics wants the ghost classified so the
  report can show "ghosts of type X").
