# Object classification

The `devicesDetails` payload returns one row per logical object. The
library classifies each row into either a `SensorKind` (sensor-side
platforms) or an `InputKind` (binary/boolean platforms), or leaves it
unclassified. Classification keys on two fields: `typ_komponentu` (the
object type) and `interpretacja` (a refinement for analog inputs).

Authoritative source:
[`src/ampio_mqtt/const.py`](../src/ampio_mqtt/const.py)
(`classify_object`, `classify_input`, `_LIN_WEJ_BY_INTERP`,
`SENSOR_TYPES`, `INPUT_TYPES`, `SYSTEM_TYPES`, `NON_SENSOR_TYPES`).

## `typ_komponentu` truth table

| `typ_komponentu`  | Sensor? | Input? | System? | Note                                                                                                                       |
| ----------------- | ------- | ------ | ------- | -------------------------------------------------------------------------------------------------------------------------- |
| `temp`            | yes     | no     | no      | Temperature reading, °C.                                                                                                   |
| `lin_wej`         | yes     | no     | no      | Analog input - kind set by `interpretacja` (see below).                                                                    |
| `bit32`           | yes     | no     | no      | Generic 32-bit measurement (units unknown).                                                                                |
| `flaga`           | no      | yes    | no      | Generic boolean flag (logic flag, button-press hold, etc.).                                                                |
| `detekcja`        | no      | yes    | yes     | Motion-style detection. Always visible.                                                                                    |
| `symulacja`       | no      | yes    | yes     | Presence simulation. Always visible. Raw-channel prefix not yet bridged.                                                   |
| `przekaznik`      | no      | no     | no      | Relay output - future `switch` platform.                                                                                   |
| `rgbw`, `led`     | no      | no     | no      | Color/light outputs - future `light` platform.                                                                             |
| `roleta_procenty` | no      | no     | no      | Roller percentage - future `cover` platform.                                                                               |
| (anything else)   | "value" | no     | no      | Generic value-only sensor with no state class - the fallback used on restricted-account installs where metadata is sparse. |

## `lin_wej` interpretation table

| `interpretacja` | Kind                | Unit  | Device class           |
| --------------- | ------------------- | ----- | ---------------------- |
| 1               | humidity            | `%`   | `humidity`             |
| 2               | pressure (absolute) | `hPa` | `atmospheric_pressure` |
| 3               | loudness            | `dB`  | `sound_pressure`       |
| 4               | illuminance         | `lx`  | `illuminance`          |
| 5               | air quality index   | -     | `aqi`                  |
| 6               | pressure (relative) | `hPa` | `pressure`             |
| 7               | CO2                 | `ppm` | `carbon_dioxide`       |

Unknown `interpretacja` values fall through to a generic `analog_<n>`
SensorKind with no device class, so a future M-SENS variant still
surfaces as a sensor.

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
