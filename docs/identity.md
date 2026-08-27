# Identity, replacement, and visibility

The biggest trap for any consumer is the choice of id. The M-SERV exposes
several ids with very different stability properties. Replacement is the
relevant axis. When a module is physically swapped, which fields stay the same,
and which do not?

Authoritative source: [`src/ampio_mqtt/models.py`](../src/ampio_mqtt/models.py).

## Modules

| Field                     | Stable across module replacement?                                                  | Use it for                                                                                           |
| ------------------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `id` (DB autoincrement)   | **No** - assigned in `mac_global` order, reassigned when a module is replaced.     | Cross-referencing objects to their owning module _within a single discovery snapshot_ only.          |
| `mac` (Designer override) | **Yes** - re-stamped onto the replacement unit so CAN logic elsewhere stays valid. | The replacement-stable per-module key. Also what the raw `ampio/from/<MAC>/...` topics are keyed by. |
| `mac_global` (factory id) | **No** - factory-burned, unique per physical unit, changes on swap.                | Display in diagnostics, never as identity.                                                           |

The M-SERV's default `mac` is `1`, which is not unique. Treat `mac` as unique
_within a single install_ (the user assigns the overrides), not globally.

`typ_urzadzenia` also derives two decoration fields on `AmpioModule`. `model` is
the product name from the vendored catalogue. `mounting` is the curated
form-factor class: `cabinet` (DIN rail), `wall` (panels, sensors, outdoor field
devices), or `flush` (in-box `-p` modules), with None for virtual, bridge-only,
handheld, and unknown codes. The classification follows Ampio's naming
convention, which no official source asserts, so it is a hand-curated table
(`device_types.MODULE_MOUNTING`). Both fields decorate device info only. The
device topology must never branch on them.

## Objects

| Field                     | Stable across module replacement?                                                                                                                         | Notes |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `id` / `device_id`        | **No** - DB autoincrement, change with the module.                                                                                                        |
| `funkcja` (channel index) | **Yes** - part of the reloaded Designer config. Not unique: if the same physical signal is exposed as several Designer objects, they share one `funkcja`. |
| `typ_komponentu`          | **Yes** - the type vocabulary (`temp`, `lin_wej`, `flaga`, ...).                                                                                          |
| `leaf_id`                 | **Yes**, when set. The wire-side visibility marker and the stable unique-id source - see below.                                                           |

## Stable unique id: `leaf_id` (`AmpioObject.stable_key`)

The recommended per-object unique id is the Designer `leafId`, exposed as
`AmpioObject.stable_key` (`leaf_<leaf_id>`) and scoped per server by the
consumer:

```
{prefix}_leaf_{leaf_id}
```

`prefix` is a per-M-SERV scope: use `AmpioServerInfo.key`, the canonical decimal
form of the server's own CAN mac. Every account tier receives it, and it is
guaranteed present once `wait_for_initial_discovery()` returns True. Why
`leaf_id` rather than the module-mac composite
(`{prefix}_obj_{module.mac}_{typ_komponentu}_{funkcja}`):

- **Available on both account tiers.** The composite needs `module.mac`, which
  only administrator accounts receive (`config/devices` does not answer for
  standard accounts, and the app-sync catalogue carries no module list).
  `leafId` ships in both the `config` and app-sync `data` catalogues with
  identical values. An install that upgrades a standard account to an
  administrator thus keeps every unique id.
- **Replacement-stable.** `leafId` is part of the reloaded Designer config, like
  `funkcja` and the `mac` override.
- **Unique among visible objects.** Across a full admin catalogue there is no
  duplicate `leafId` once the `visible` filter is applied. The known collision
  is a hidden phantom stub that shares its labeled twin's `leaf_id`. The
  `hidden` flag removes exactly that stub, so filter on `visible` before you
  key. (Live installs can carry several such pairs at once, all on M-SENS analog
  channels.) If a user deliberately exposes one physical signal as several
  visible Designer objects, those share a `leafId`. The same shape collides the
  composite too (same mac, typ, and funkcja), so `leaf_id` is never worse.

Objects with an empty `leaf_id` still need a fallback key, because system
objects (`symulacja`, `detekcja`) are visible without one. On the admin tier the
module-mac composite above still covers them. On the standard tier only the DB
`id` is available, with its replacement instability accepted and documented.

## Module identity on every tier: `AmpioObject.module_mac`

`leafId` embeds the owning module's override mac as its second segment
(`0_<macHex>_...`), exposed as `AmpioObject.module_mac`. The embedded value
equals `AmpioModule.mac`, live-verified across a full catalogue, including the
M-SERV, whose override (`1`) diverges from its factory id. A consumer can thus
group entities by physical module even on the restricted tier, which never
receives the module catalogue. An entry created with a standard account and
later switched to an administrator keeps its entity-to-device mapping and only
gains metadata. The parse is strict: any shape other than
`0_<macHex>_<F2>_<F3>_<F4>` reads as None, exactly like the empty `leafId` of
system objects and ghost rows.

Three helpers close the loop for a consumer that builds devices on `module_mac`.
`AmpioObject.is_server_owned` marks the objects that belong to the M-SERV itself
(their `leafId` embeds its override mac), so they anchor to the hub device
identically on both tiers. `AmpioClient.mserv` returns the M-SERV's own module
row - name, model, versions - on the admin tier that has the catalogue.
`AmpioClient.module_for(obj)` resolves any object to its catalogue row. It joins
on `device_id` and gates on mac agreement, so the volatile DB join can never
pair an object with a replaced module's stale row. The join keys the lookup
rather than the mac, because override macs can collide across rows. The mac then
gates what the join found.

## The F segments (`0_<macHex>_<F2>_<F3>_<F4>`)

The library parses only the mac and the trailing `F4` (the 0-based output index,
`AmpioObject.leaf_out_no`). `F2` and `F3` stay unparsed. A live join of every
leaf-bearing object against the module catalogue pins down what they hold.

**`F2` is a per-leaf class code, not the module type.** No module showed `F2`
equal to its `typ_urzadzenia`. Virtual cover objects hosted on a relay module
carry the roller code, so the code follows the configured leaf class, not the
host product. The low codes match the Designer bundle's IO type enum exactly
where both are known:

| `F2`  | Observed leaf class                                             |
| ----- | --------------------------------------------------------------- |
| 3     | binary flag (`flaga`)                                           |
| 5     | roller (`roleta_*`)                                             |
| 13    | heating regulator (`reg`)                                       |
| 30    | `rgbw`                                                          |
| 67    | open-collector output (`led`/`przekaznik` on M-INOC)            |
| 73-76 | M-SENS channels (`lin_wej`, `temp`)                             |
| 257   | binary I/O                                                      |
| 296   | `satel_alarm`                                                   |
| >1000 | bridged/wireless leaves: 1001 `temp`, 1002 `bit8`, 1005 `bit32` |

**`F3` is a role discriminator within the class**, not a bank index. Class 257
carries inputs as `F3` 1 (`wej`) and outputs as `F3` 2 (`przekaznik`). The two
`satel_alarm` roles (armed, alarmed) ride 296 as `F3` 3 and 4. Every single-role
class observed uses 0.

`F2` thus carries a tier-independent function-class signal (it rides the
app-sync catalogue the restricted tier receives), but it cannot replace the
module type code. The table above is observed coverage, not a specification, so
an unlisted code proves nothing. The library keeps its classification on
`typ_komponentu` alone.

## Visibility (`AmpioObject.visible`)

Not every row in `devicesDetails` is meant to be surfaced. The predicate is:

```
visible = not hidden and (bool(leaf_id) or is_system)
```

- **`hidden`** - `params` bit 4 (`params & 16`). The M-SERV's own authoritative
  "do not surface" marker, and it takes precedence over everything else. It
  marks phantom rows that duplicate a real Designer channel (same `leaf_id`, no
  value), and it marks objects the user hid. Those are exactly the rows the
  `leaf_id` test alone wrongly keeps. It is a Designer config flag, so unlike
  the DB `id` it is replacement-stable. Every account tier receives `params` on
  a baseline server (via `devicesDetails` or `data/params_devices`). A row
  without a received value reads `0`, so `hidden` is False and the `leaf_id`
  test alone decides. This is the same gate the M-SERV's Matter bridge uses
  (`(params & 2**37) && !(params & 16)`) - see the section on the bit semantics
  below. Bit 37 is a Matter-only opt-in. The library deliberately does not
  filter on it and does not surface it.
- **`leaf_id`** - non-empty for every "real" object in the M-SERV's view. Empty
  for **ghost rows** (objects the user deleted in Designer but that still come
  back over the wire) and for **system objects** (presence simulation,
  detection). Use `leaf_id` non-empty as the default visibility filter.
- **`is_system`** - `typ_komponentu in {symulacja, detekcja}`. Always visible
  regardless of grouping. The M-SERV exposes these unconditionally.

Treat `visible` as the discovery filter. Ghosts that slip in look like real
entities until the user notices that their HA counterpart no longer exists in
Designer.

## Where the `params` bit semantics come from

The Designer web bundle embeds the enum that names every bit of the object
`params` integer:

```text
SHOW_ACTIVE:1             DALI_OBJECT:2              DALI_GROUP:4
OWA_OBJECT:8              DELETED:16                 MAKE_SEMICOLON:32
READ_ONLY:64              BLOCK_LOCAL:128            BLOCK_REMOTE:256
HIDE_DESC_ON_SKETCH:512   BLOCK_LOGGING:1024         PRESENCE_DETECT_ENT:2048
PRESENCE_DETECT_INS:4096  REDIRECT_USING_OLD_CLOUD:8192
HIDE_TITLE:16384          OPTION1:32768              SHOW_CONNECTED_AS_LIST:65536
ADD_UNIT_TO_DESC:131072   ADD_DESC_TO_ICON:262144    ADD_VALUE_TO_ICON:524288
CUSTOM_RANGE:2^20         REVERSE_ROLLERS:2^21       USE_IN_WEATHER:2^22
HIDE_LOADER:2^23          HIDE_ADDITIONAL_OPTIONS:2^24
OPTION2:2^25              OPTION3:2^26               OPTION4:2^27
HIDE_IN_LOGBOOK:2^28      STEP_OBJECT:2^29           OPTION5:2^30
OPTION6:2^31              INCREMENTAL:2^32           HIDE_MIN_MAX:2^33
KNX_VALUE:2^34            LORA_VALUE:2^35            HIDE_LAST:2^36
MATTER:2^37               USE_ONLY_VALUE_FROM_RANGE:2^38
SHOW_AT_FULL_WIDTH:2^39
```

The `OPTION1` through `OPTION6` slots are generic. Their meaning depends on the
component type, and the Designer editor renders each with a per-type label. For
`OPTION1` (bit 15): "Bell object" on `przekaznik` and `flaga`, "show switch in
slider" on slider-shaped outputs, "1% lamella" on tilt covers, "block
heating/cooling change" on `reg`, and other labels on camera, webview, and alarm
objects. A reader of an OPTION bit must gate on the component type first.

The library reads three of these bits. `DELETED` (bit 4) backs `hidden` and
`visible`. `READ_ONLY` (bit 6) backs `read_only`. `OPTION1` (bit 15) backs
`bell`, gated on the two component types the label applies to.

A bell object is meant for a single press. The Ampio app renders it as a
press-only button instead of a toggle. The checkbox is display intent: it sets
bit 15 and nothing else, and whether the output auto-releases is the module's
own configuration. The marker is readable on both account tiers, because
`data/params_devices` serves `params` unfiltered.

The M-SERV ships its own Matter bridge (a matter.js app launched by
`ampio-server`). That bridge's production gate corroborates the enum: it exposes
an object only when `(params & 2**37) && !(params & 16)`. Bit 37 is the
per-object Matter opt-in set in Designer. Bit 4 is the hidden/stub marker that
`hidden` and `visible` build on. The `leafId` structure
`0_<macHex>_<F2>_<F3>_<F4>` that `AmpioObject.module_mac` parses is likewise the
structure the bridge's own classifier reads. The bridge also shows why a
dedicated integration is the right path for sensors. It types objects through a
registry with known gaps (no `lin_wej` branch, and loudness has no Matter device
type at all). It keys endpoints on the volatile DB `id`. And it exposes only the
channels hand-flagged for Matter - a dozen on the reference install, with
humidity, pressure, illuminance, and CO2 on zero modules.

## The read-only marker (`AmpioObject.read_only`)

Designer has a per-object "read only" checkbox. The checkbox sets `params` bit 6
and nothing else. A live probe on a flag pinned down the behavior:

- The M-SERV enforces the marker itself, on both account tiers. An `/api` write
  to a read-only object produces no echo and no error. A watch on `hw/out`
  during the write shows why. The M-SERV emits zero CAN frames for the read-only
  object. The same write to a writable flag emits the normal frame set. Reads
  are unaffected on every surface.
- The marker never reaches the module. The CAN description record is identical
  for a read-only flag and a writable one, so only the catalogue `params` field
  announces it.
- The restricted tier can detect it. `data/params_devices` is served unfiltered,
  so `params` is available even for objects outside the grant.

The checkbox can change at any time in Designer. While `read_only` is True, a
consumer must reject writes and keep the entity's platform stable. In Home
Assistant, keep the entity a switch and raise an error on the service call. Do
not rebuild it as a binary sensor. A platform swap breaks the entity id, its
history, and every automation on each checkbox change.

Deletion behaves as follows on the wire, on the baseline server. A **module**
delete hard-removes its row from the `devices` list (the library evicts it and
dispatches `ModuleRemoved`), but it does not cascade to the module's objects. An
**object** delete in the Ampio app is two-stage: the object first moves to
"Ungrouped", and a second delete purges it. On the `config` catalogue the purge
is soft. The row stays, `leaf_id` intact, with the `params` hidden bit set, so
it drops out through `visible`. The app-sync surfaces (`data/devices`,
`data/params_devices`) hard-remove it, and that is what lets the restricted tier
evict for real.

## The Matter device type tag (the `type` column)

The Designer "Description in device" panel lets the installer tag an output with
a Matter device type ("Lighting - On-off light", "Plugs - Pump", and so on). The
tag lives in the module itself, as one entry of the per-output description
record `{descType, outNo, outLoc, outType, desc}`. Designer writes that record
over `device_api/to/<macHex>/descriptions_wr` (base64 frames of
`[len:2][descType:2][outNo:2][outLoc:2][outType:2][utf8 desc]`, little-endian).
It also mirrors `outType` into the object row's `type` column on both
catalogues, as a decimal string (`"256"` = 0x0100). The library parses that
mirror into `AmpioObject.matter_device_type`. That field is a pure catalogue
fact. The sweep never changes it.

Assignment and exposure are two independent facts. `type` is the device-type
assignment. `params` bit 37 is the Matter-bridge exposure opt-in, and a row can
carry a `type` with bit 37 clear. The tag is installer intent. It is the one
wire signal that separates a relay for a light from one for a plug or a pump. It
is also opt-in per output: untagged rows read `None`, so `kind` (from
`typ_komponentu`) stays the fallback classification.

The vocabulary is the standard Matter device type table, exactly as the Designer
web bundle embeds it:

| Group                 | Device types                                                                                                                                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Lighting              | 0x0100 On-off light, 0x0101 Dimmable light, 0x010C Color temperature light, 0x010D Extended color light                                                 |
| Plugs                 | 0x010A On-off plug-in unit, 0x010B Dimmable plug-in unit, 0x0303 Pump                                                                                   |
| Switches and controls | 0x0103 On-off light switch, 0x0104 Dimmer switch, 0x0105 Color dimmer switch, 0x0304 Pump controller, 0x000F Generic switch                             |
| Sensors               | 0x0015 Contact, 0x0106 Light, 0x0107 Occupancy, 0x0302 Temperature, 0x0305 Pressure, 0x0306 Flow, 0x0307 Humidity, 0x0850 On-off, 0x0076 Smoke/CO alarm |
| Closures              | 0x000A Door lock, 0x000B Door lock controller, 0x0202 Window covering, 0x0203 Window covering controller                                                |
| HVAC                  | 0x0300 Heating/cooling unit, 0x0301 Thermostat, 0x002B Fan, 0x002D Air purifier, 0x002C Air quality sensor                                              |

The CAN-resident description record is authoritative for the tag. The `type`
column mirror lags it. An output tagged 256 (0x0100) on the CAN record can still
show an empty `type` column, live-observed on the reference install.
`AmpioClient.resolve_records()` reads the CAN record into `AmpioObject.record`:
the tag lands in `record.matter_device_type`, the location pointer in
`record.location`, and the entry's own description string in `record.name`. The
column mirror stays in `matter_device_type`, identical on both tiers. The two
fields are separate facts. The consumer picks which one to trust.

## The Designer location (per-output `outLoc`)

The Designer "Lokalizacja" dropdown sits on an output's "Description in device"
panel. It writes a second pointer into the same per-output description record as
the Matter tag above: `{descType, outNo, outLoc, outType, desc}`. `outLoc`
indexes the locations name table (request keyword `locations` on the admin
`config` surface, `{id, opis_menu, opis_rozwiniety}` rows), and 0 means
unassigned. A read-back needs the same CAN-resident record the Matter tag lives
in, because Designer does not mirror `outLoc` to the object catalogue. The DB
row's `lokalizacja` column reads 0 for every object on the reference install.
`outType` differs: the `type` column does mirror it, with the lag noted above.

### The get_data request/reply pair

Request: an empty payload to `device_api/to/<machex>/get_data`, mac in lowercase
hex. Reply: JSON on `device_api/from/<MACHEX>/info`, mac in UPPERCASE hex on the
wire - parse the topic segment with `int(x, 16)`, never compare strings. The
reply's `descriptions` field is base64 of the module's full description record.

The blob decodes into repeated little-endian frames:

```
[len:2][descType:2][outNo:2][outLoc:2][outType:2][utf8 desc]
```

`len` counts the whole frame, header included. A frame whose `len` is below the
10-byte header ends the walk, and so does a frame that runs past the end of the
blob. The remainder is unreadable either way.

A cleared entry stays in the record. Designer rewrites the frame in place with
`outLoc` 16383, `outType` 0, and the placeholder description `.`. The frame
count never changes, live-proven on the reference install. The library reads all
three sentinel values as absent.

`descType` is the description class the frame belongs to (the Designer web
bundle's enum):

| Value | Name                                                                 |
| ----- | -------------------------------------------------------------------- |
| 1     | DEVICE_NAME                                                          |
| 3     | OW                                                                   |
| 6     | FLAG_BIN                                                             |
| 7     | FLAG_U8                                                              |
| 8     | FLAG_I16                                                             |
| 10    | INPUTS                                                               |
| 12    | OUTPUTS                                                              |
| 14    | IN_U8                                                                |
| 15    | MLED                                                                 |
| 16    | OUT_OC_U8                                                            |
| 17    | MRT                                                                  |
| 20    | SCREEN_NO                                                            |
| 22    | FLAG_BIN_SIMPLE                                                      |
| 23    | SatelZone                                                            |
| 24    | SatelInput                                                           |
| 25    | SatelOutput                                                          |
| 26    | ROLLER                                                               |
| 34    | (the RGBW output class - no symbolic name recovered from the bundle) |

### The module-level record (`AmpioModule.record`)

The record's one DEVICE_NAME frame (descType 1) describes the module itself. Its
`desc` is the module name, and its `outLoc` is the module-level "Lokalizacja" -
where the module is mounted, not where its loads are. `resolve_records()` reads
it from the same reply and sets `AmpioModule.record`, with a `ModuleUpdated`
dispatch on change. `record.location` is the mounting location and `record.name`
the CAN-resident module name. A record without the frame, or with `outLoc` 0,
reads unassigned (None). The module answered, so None is authoritative. A module
the sweep did not cover keeps its previous value, exactly like the per-object
side. On the reference install the installer tagged wall devices this way (an
M-SENS and three M-DOT panels carry room names) and left the cabinet modules
untagged.

### The join rule

An object joins its entry through
`(DESC_TYPE_BY_KIND[typ_komponentu], leaf_out_no)` within the description record
of its own module (`AmpioObject.module_mac`). `leaf_out_no` is the last `leafId`
segment. It is live-proven as the out-no key over `funkcja` across the full
catalogue: `funkcja` under- or over-matches, dependent on the kind, and
`leaf_out_no` does not. `DESC_TYPE_BY_KIND` ships only live-proven pairs:
`przekaznik` -> 12 (OUTPUTS), `roleta_procenty` -> 26 (ROLLER), `roleta_lamelki`
-> 26 (ROLLER), `led` -> 16 (OUT_OC_U8), `rgbw` -> 34. A kind outside that table
(`bit32`, `flaga`, `lin_wej`, `satel_alarm`, `temp` among them) resolves no
location. On the full-catalogue probe, each of those landed on two or more
descTypes with a cleared majority at once. The join for such a kind is thus
ambiguous, not merely unproven by sample size.

### Tier gate

The whole `device_api` tree is admin-only, exactly like the raw tree. A
restricted account gets silence on both the subscribe and the request.
`AmpioClient.resolve_records()` raises `RuntimeError` with the tier in the
message, instead of a hang on a reply that never comes.
