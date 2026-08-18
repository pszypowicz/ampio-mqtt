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

Authoritative source:
[`src/ampio_mqtt/classification.py`](../src/ampio_mqtt/classification.py)
(`classify`, the `TYPE_PROFILES` table, and `_LIN_WEJ_BY_INTERP`).

## `typ_komponentu` truth table

Each row here is one `TYPE_PROFILES` entry in `classification.py`: its
`kind` (a fixed kind instance, or the analog/numeric selector for the
`interpretacja`-keyed families) plus the `system` flag - keep that table
in sync when a new type is added.

| `typ_komponentu`                              | Sensor? | Input? | System? | Note                                                                                                                                       |
| --------------------------------------------- | ------- | ------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `temp`                                        | yes     | no     | no      | Temperature reading, °C.                                                                                                                   |
| `lin_wej`                                     | yes     | no     | no      | Analog input - kind set by `interpretacja` (see below).                                                                                    |
| `bit32`                                       | yes     | no     | no      | Generic 32-bit measurement (units unknown).                                                                                                |
| `bit8`                                        | yes     | no     | no      | Generic 8-bit measurement, same treatment as `bit32`.                                                                                      |
| `reg`                                         | no      | no     | no      | Temperature controller (`ThermostatKind`, climate platform) - state is the running flag; #73 tracks the rich readback.                     |
| `flaga`                                       | no      | yes    | no      | Generic boolean flag (logic flag, button-press hold, etc.).                                                                                |
| `detekcja`                                    | no      | yes    | yes     | Motion-style detection. Visible even without `leafId` (unless hidden).                                                                     |
| `symulacja`                                   | no      | yes    | yes     | Presence simulation. Visible even without `leafId` (unless hidden). Raw-channel prefix not yet bridged.                                    |
| `przekaznik`                                  | no      | no     | no      | Relay output - see the output table below.                                                                                                 |
| `rgbw`, `led`                                 | no      | no     | no      | Light outputs - see the output table below.                                                                                                |
| `roleta`, `roleta_procenty`, `roleta_lamelki` | no      | no     | no      | Cover outputs - see the output table below.                                                                                                |
| (anything else)                               | "value" | no     | no      | Generic value-only sensor with no state class - the fallback for objects whose metadata has not arrived or whose type is not in the table. |

## Output truth table

Controllable types get an `OutputKind` whose flags say which command
verbs the object answers, so a consumer picks a platform and feature set
without its own `typ_komponentu` table. The `switchable` flag covers the
`turnOn` / `turnOff` / `switch` family: every output answers it except
`rgbw`, which the M-SERV drives through `setColors` alone. The library's
`turn_off()` sends `setColors 0/0/0/0` for it, while `turn_on()` and
`toggle()` raise `ValueError` - turning a color light on means choosing
a color, which is the consumer's call via `set_color()`.

| `typ_komponentu`  | `OutputKind.key` | Dimmable | Color | Cover | Position | Tilt | Switchable | Platform shape                    |
| ----------------- | ---------------- | -------- | ----- | ----- | -------- | ---- | ---------- | --------------------------------- |
| `przekaznik`      | `relay`          | no       | no    | no    | no       | no   | yes        | switch                            |
| `led`             | `dimmer`         | yes      | no    | no    | no       | no   | yes        | light with brightness             |
| `rgbw`            | `rgbw`           | no       | yes   | no    | no       | no   | no         | light with RGBW colour            |
| `roleta`          | `cover`          | no       | no    | yes   | no       | no   | yes        | cover, open/close/stop only       |
| `roleta_procenty` | `cover_position` | no       | no    | yes   | yes      | no   | yes        | cover with position               |
| `roleta_lamelki`  | `cover_tilt`     | no       | no    | yes   | yes      | yes  | yes        | cover with position and slat tilt |

`roleta_lamelki` is what the Ampio app writes when a cover's type is set
to "blinds - slats"; the same cover reads back as `roleta_procenty` while
it is set to "blinds - percentage". Only the slats variant reports a
`lammel` angle in its state payload, surfaced as
`AmpioObject.tilt_position`.

Ampio's own vocabulary also carries `rgb`, `rgbww`, `ledww`, `ac`,
`radio`, `ip_radio`, and `satel_alarm` - types absent from
`TYPE_PROFILES` that classify as the generic value sensor.
`satel_alarm` is the armed/alarmed flags of an alarm integration (a
Jablotron behind an M-CON, so the prefix is not Satel-specific).

## `lin_wej` interpretation table

For a `lin_wej` object the measurement is selected by `interpretacja`
(`_LIN_WEJ_BY_INTERP` in
[`src/ampio_mqtt/classification.py`](../src/ampio_mqtt/classification.py)):

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
