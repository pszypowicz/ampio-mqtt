# The M-SERV Matter bridge (reverse-engineered)

The M-SERV ships its own Matter bridge. It is not part of this library, but
understanding it pins down two things this library depends on: the meaning of
the `params` bitfield (which drives `AmpioObject.hidden` / `visible`), and why a
dedicated Home Assistant integration is needed for sensors rather than leaning
on the bridge.

Everything here was reverse-engineered (read-only) from the bridge bundle and
cross-checked against the live `obiekty` catalogue and the MQTT `devicesDetails`
payload. Bit semantics are validated on one install; the strong corroboration
is that the bridge's own production gate uses them.

## What it is

- A **matter.js** (`@matter/nodejs`) app, shipped as a single bundled CommonJS
  file (`DeviceNode20.cjs`; source map names the project `AmpioMatterBridge`).
- Launched by `ampio-server` (not a standalone systemd unit), with
  `--storage-path`, `--uniqueid`, `--interface`. When commissioned it exposes a
  root -> `aggregator` endpoint with one **bridged device per exposed object**.
- It reads the same object catalogue this library reads. Per object it sets the
  Matter endpoint id to `"{id} - {funkcja}"`, the endpoint number to the DB
  `id`, and `nodeLabel` to `opis_menu` (truncated to 32 chars). Identity is the
  volatile DB `id` - fine for the bridge (re-derived every boot, co-located with
  the DB), but not replacement-stable, which is why this library keys on
  `{mac, typ_komponentu, funkcja}` instead.

## The selection gate (origin of `params` bit 4 / bit 37)

The bridge classifies an object only if it passes:

```js
const M = 2n ** 37n;                       // matter-exposed flag
if ((BigInt(J.params) & M) > 0 && (J.params & 16) === 0) { ... }
```

- **bit 37** (`params & 2**37`) - "expose this object to Matter". A per-object
  opt-in the user sets in Designer. Surfaced here as `AmpioObject.matter_exposed`
  and never used for filtering (most real objects leave it clear).
- **bit 4** (`params & 16`) - "hidden / stub". `!(params & 16)` is the bridge's
  general "not hidden" test. Surfaced here as `AmpioObject.hidden` and used by
  `visible`. It is the authoritative marker that drops phantom rows and
  user-hidden objects; it is replacement-stable (a Designer config flag, not the
  DB id). See [`identity.md`](identity.md).
- **bit 0** is set on every object on the reference install (no signal).

## Classification: `ARe` then `RRe`

```js
let Q = ARe(Number(J.type)); // (a) by numeric Matter type
if (!Q) Q = RRe(J.typ_komponentu, J.leafId); // (b) fallback
```

### `ARe(type)` - "findDeviceByType" (primary)

```js
function ARe(t) {
  return uo.find((e) => e.deviceType === t);
}
```

`uo` is the registry of supported Matter device types; `ARe` returns the one
whose `deviceType` equals the object's numeric `type` field. `type` is a Matter
device-type ID **hand-set in Designer**, so an object carries one only where the
user assigned it. On the live reference install relays (`256`/`266`), lights
(`257`/`269`), flags (`266`), air-quality (`44`), and temperature (`770`)
objects carry one; most analog/environmental channels (humidity, pressure,
loudness, illuminance, CO2) leave it empty.

### `RRe(typ_komponentu, leafId)` - "findDeviceByComponent" (fallback)

A `typ_komponentu` switch, then a `leafId` table. The Matter device-type
numbers below were resolved by chasing the bundle's alias chains and confirmed
against `name:"…"` definitions.

`typ_komponentu` switch:

| `typ_komponentu`                              | Matter device      | `deviceType`  |
| --------------------------------------------- | ------------------ | ------------- |
| `roleta`, `roleta_lamelki`, `roleta_procenty` | WindowCovering     | 514 (0x0202)  |
| `reg`, `ac`                                   | Thermostat         | 769 (0x0301)  |
| `temp`                                        | TemperatureSensor  | 770 (0x0302)  |
| `przekaznik`, `flaga`                         | **OnOffLight**     | 256 (0x0100)  |
| `led`, `flaga_liniowa`, `flaga_liniowa16`     | DimmableLight      | 257 (0x0101)  |
| `rgb`, `rgbw`, `rgbww`, `ledww`               | ExtendedColorLight | 269 (0x010D)  |
| `wej`                                         | OnOffSensor        | 2128 (0x0850) |
| `radio`, `ip_radio`                           | Speaker            | 34 (0x0022)   |

`leafId` table (only reached when the switch misses, e.g. for `lin_wej`).
`leafId` is `0_<macHex>_<F2>_<F3>_<F4>`; the table reads **F2** and **F4**
(F3 is parsed into a variable and then never used):

| F2       | F4  | Matter device                  | `deviceType`     |
| -------- | --- | ------------------------------ | ---------------- |
| 72 or 73 | 0   | HumiditySensor                 | 775 (0x0307)     |
| 72 or 73 | 1   | PressureSensor                 | 773 (0x0305)     |
| 74       | 0   | LightSensor (illuminance)      | 262 (0x0106)     |
| 74       | 1   | AirQualitySensor               | 44 (0x002C)      |
| 74       | 2   | PressureSensor                 | 773 (0x0305)     |
| 77       | any | LightSensor                    | 262 (0x0106)     |
| 75       | any | AirQualitySensor + CO2 numeric | 44 + CO2 cluster |

The `75` branch returns an AirQualitySensor decorated with a
`CarbonDioxideConcentrationMeasurementServer` (`NumericMeasurement`).

## Quirks and gaps

- **No `lin_wej` branch** in the `typ_komponentu` switch - every M-SENS analog
  channel falls through to the `leafId` table (or needs a hand-set numeric
  `type`). The string `lin_wej` does not appear anywhere in the bundle.
- **The `leafId` table is effectively dead code on the reference install.**
  Every Matter-exposed sensor there is answered earlier in the chain -
  air-quality by `ARe` (its `type=44`), temperature by the `typ_komponentu`
  switch (the `temp` branch, since most exposed temperature objects leave `type`
  empty) - so the `(F2,F4)` table is never reached.
- **F3 is parsed but unused** - only `(F2, F4)` decide the type.
- **No `(73, 2)` rule -> Loudness / sound-pressure is unmappable.** Matter has
  no sound-pressure device type, and the bridge has no branch for it, so a
  loudness channel returns `undefined` and is dropped. (Latent trap: the only
  `o=2` rule is `(74, 2) -> PressureSensor`; a loudness channel mis-authored as
  `74_0_2` would be silently typed a pressure sensor.)
- **No `F2=76` rule** - temperature in the leafId path would miss it, but
  `typ_komponentu === "temp"` and a hand-set `type=770` both catch it first, so
  temperature is the one environmental channel that is triple-covered.
- **`przekaznik`/`flaga` -> OnOffLight (256)**, not a generic on/off plug, and
  **`wej` -> OnOffSensor (2128)**. Relays therefore appear in Matter as lights.

## What the bridge actually exposes (live reference install: 39 modules, 134 objects)

Only **12 environmental channels** reach Matter: 6 air-quality (`type=44`) + 6
temperature. **Humidity, both pressure channels, Loudness, Brightness/Illuminance,
and CO2 are exposed on zero modules** - their objects have the matter-exposed
flag clear (`params` bit 37 unset), so they are dropped at the gate before any
classification. Loudness could not be exposed even if flagged.

## Implications for this library and the HA integration

- **Visibility / identity:** `params` bit 4 is the authoritative hidden marker;
  this library uses it in `hidden` / `visible`. Bit 37 is Matter-only and is
  surfaced as `matter_exposed` for information, never for filtering.
- **Sensor typing:** this library types analog channels from `interpretacja`
  (the complete `lin_wej` map: humidity, pressure, loudness, illuminance, IAQ,
  CO2 - see [`classification.md`](classification.md)), which is more complete
  than the bridge's gappy `leafId` table. It can type all M-SENS environmental
  channels, including loudness (as sound-pressure), which Matter cannot
  represent at all.
- **Conclusion:** the M-SERV Matter bridge surfaces only the handful of channels
  a user hand-flags for Matter, and types them via a registry that has gaps. A
  dedicated integration that reads the Ampio objects directly is the right path
  for complete, correctly-typed sensor coverage.

## How this was verified

- Read of the bridge bundle (the `ARe`/`RRe` definitions, the device-type symbol
  alias chains, and the selection gate).
- The live `obiekty` catalogue (the `type`/`params`/`leafId` columns per object).
- The MQTT `devicesDetails` payload (confirming `params` is on the wire, so this
  library can read it).

Caveat: the bit semantics are reverse-engineered and validated against a single
install; they match the bridge's own production gate, but a second catalogue
would be worth confirming before treating them as guaranteed.
