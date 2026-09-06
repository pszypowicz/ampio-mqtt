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

| Field                     | Stable across module replacement?                                                                                                                                                                                                                                                | Notes                                                                                |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `id`                      | **Yes**, in practice. An object delete is soft on the `config` catalogue. The row stays, with the `params` hidden bit set, so the autoincrement never renumbers. Observed unchanged on a live install across years of configuration uploads, module replacements, and deletions. | The per-object unique id, exposed as `AmpioObject.object_key`.                       |
| `id_urzadzenia`           | **No** - it mirrors the module row, which is reassigned in `mac_global` order when a module is replaced.                                                                                                                                                                         | Cross-referencing an object to its module _within a single discovery snapshot_ only. |
| `funkcja` (channel index) | **Yes** - part of the reloaded Designer config. Not unique: if the same physical signal is exposed as several Designer objects, they share one `funkcja`.                                                                                                                        |
| `typ_komponentu`          | **Yes** - the type vocabulary (`temp`, `lin_wej`, `flaga`, ...).                                                                                                                                                                                                                 |
| `leaf_id`                 | **Yes**, when set. The physical-output key source and the parse source for `module_mac`. Empty for system objects and after a Matter uncheck - see below.                                                                                                                        |

## Unique id: the object id (`AmpioObject.object_key`)

The recommended per-object unique id is the database object id, exposed as
`AmpioObject.object_key` (`obj_<id>`) and scoped per server by the consumer:

```
{prefix}_obj_{id}
```

`prefix` is a per-M-SERV scope: use `AmpioServerInfo.server_key`, the canonical
decimal form of the server's own CAN mac. Every account tier receives it, and it
is guaranteed present once `wait_for_initial_discovery()` returns True.

Three properties make the object id the right source:

- **Unique, always.** One id belongs to one catalogue row. No filter and no
  fallback are needed, so leafless rows and hidden rows key exactly like every
  other object. `visible` remains the discovery filter, and this uniqueness does
  not depend on it.
- **Available on both account tiers.** The id is the key of every catalogue
  surface, so a standard account and an administrator account agree on it.
- **Stable in practice.** Designer soft-deletes. The `params` `DELETED` bit
  marks a removed object, and the row stays. The autoincrement therefore never
  has to renumber, and a live install has kept every id across years of
  configuration uploads, module replacements, and deletions.

## Physical-output key: `leaf_id` (`AmpioObject.leaf_key`)

`AmpioObject.leaf_key` (`leaf_<leaf_id>`) names the physical output an object
drives. It is not an identity for the object row, and it must not be used as
one.

Several Designer objects can drive one output, and the Designer supports this
today. One view can act as a plain relay. Another view of the same output can
carry the bell marker and a pulse time. Every such view carries the same
`leafId`. A consumer keyed on `leaf_key` therefore sees one key for several
objects and loses all but one of them.

`leaf_key` answers three questions:

- **Which entities drive one output.** Two objects with equal `leaf_key` share a
  relay, a dimmer, or a roller.
- **The parse source.** `module_mac` and `leaf_io_no` are read out of it.
- **The join anchor.** The description-record join matches on `module_mac` and
  `leaf_io_no`.

`leafId` is empty for system objects, and Designer clears it on any object whose
Matter box is unchecked, so `leaf_key` reads None for both. An empty `leafId`
says nothing about visibility, which the section below covers.

One further collision exists and is unrelated to the Designer views above. A
hidden phantom stub can share its labeled twin's `leaf_id` on M-SENS analog
channels. The `hidden` flag removes exactly that stub, so filter on `visible`
before grouping by output.

## Module identity on every tier: `AmpioObject.module_mac`

`leafId` embeds the owning module's override mac as its second segment
(`0_<macHex>_...`), exposed as `AmpioObject.module_mac`. The embedded value
equals `AmpioModule.mac`, live-verified across a full catalogue, including the
M-SERV, whose override (`1`) diverges from its factory id. A consumer can thus
group entities by physical module even on the restricted tier, which never
receives the module catalogue. An entry created with a standard account and
later switched to an administrator keeps its entity-to-device mapping and only
gains metadata. The parse is strict: any shape other than
`0_<macHex>_<sfId>_<subSfId>_<ioNo>` reads as None, exactly like an empty
`leafId`.

Three helpers close the loop for a consumer that builds devices on `module_mac`.
`AmpioObject.is_server_owned` marks the objects that belong to the M-SERV itself
(their `leafId` embeds its override mac), so they anchor to the hub device
identically on both tiers. `AmpioClient.mserv` returns the M-SERV's own module
row - name, model, versions - on the admin tier that has the catalogue.
`AmpioClient.module_for(obj)` resolves any object to its catalogue row. It joins
on `id_urzadzenia` and gates on mac agreement, so the volatile DB join can never
pair an object with a replaced module's stale row. The join keys the lookup
rather than the mac, because override macs can collide across rows. The mac then
gates what the join found. A leafless object has no mac to gate on, so its join
stands as is. That is the one module lookup a leafless object has, and it
answers on the admin tier only.

## The leaf-id segments (`0_<macHex>_<sfId>_<subSfId>_<ioNo>`)

The Designer names all five segments. Its web bundle builds the token, and it
parses the token back into `macGroup`, `mac`, `sfId`, `subSfId`, and `ioNo`.
Earlier revisions of this page called the last three `F2`, `F3`, and `F4`.

The library parses the mac, the `sfId`, the `subSfId`, and the trailing `ioNo`.
`AmpioObject.leaf_io_no` reads the last segment. It covers inputs as well as
outputs. `AmpioObject.sf_id` and `AmpioObject.sub_sf_id` read the third and
fourth segments, next to `module_mac` and `leaf_io_no`. A `subSfId` has meaning
only inside its `sfId`. Both read None when `leaf_id` is empty or malformed.

The two tables below come from a live join of every leaf-bearing object against
the module catalogue.

**`sfId` is a per-leaf special-function id, not the module type.** No module
showed `sfId` equal to its `typ_urzadzenia`. Virtual cover objects hosted on a
relay module carry the roller code, so the code follows the configured leaf
class, not the host product. The low codes match the Designer bundle's IO type
enum exactly where both are known:

| `sfId` | Observed leaf class                                             |
| ------ | --------------------------------------------------------------- |
| 3      | binary flag (`flaga`)                                           |
| 5      | roller (`roleta_*`)                                             |
| 13     | heating regulator (`reg`)                                       |
| 30     | `rgbw`                                                          |
| 67     | open-collector output (`led`/`przekaznik` on M-INOC)            |
| 73-76  | M-SENS channels (`lin_wej`, `temp`)                             |
| 257    | binary I/O                                                      |
| 296    | `satel_alarm`                                                   |
| >1000  | bridged/wireless leaves: 1001 `temp`, 1002 `bit8`, 1005 `bit32` |

**`subSfId` selects a sub-function inside its `sfId`**, not a bank index. The
Designer nests the sub-functions under each special function, so a value has
meaning only in that scope. No global sub-function enum exists. The observed
values follow the same pattern the bundle uses:

| `sfId` | `subSfId` | Observed role         |
| ------ | --------- | --------------------- |
| 257    | 1         | input (`wej`)         |
| 257    | 2         | output (`przekaznik`) |
| 296    | 3         | alarm armed           |
| 296    | 4         | alarm alarmed         |

Every single-role class observed uses 0. The bundle's own alarm special function
names sub-function 1 as input, 2 as output, 3 as armed, and 4 as alarmed, which
is the pattern the rows above show.

`sfId` thus carries a tier-independent function-class signal (it rides the
app-sync catalogue the restricted tier receives), but it cannot replace the
module type code. Both tables are observed coverage, not a specification, so an
unlisted code proves nothing. The library keeps its classification on
`typ_komponentu` alone.

## Visibility (`AmpioObject.visible`)

Not every row in the catalogue is meant to be surfaced. The predicate is:

```
visible = not hidden
```

`hidden` is `params` bit 4 (`params & 16`), the bit the Designer enum names
`DELETED`. It is the M-SERV's own "do not surface" marker, and the one wire-side
visibility signal. It marks the rows the user deleted or hid, and the phantom
stubs that duplicate a real Designer channel (same `leaf_id`, no value). It is a
Designer config flag, so unlike `id_urzadzenia` it is replacement-stable. Every
account tier receives `params` on a baseline server, through `devicesDetails` or
`data/params_devices`. A row without a received value reads `0`, so it is
visible. This is the same gate the M-SERV's Matter bridge uses
(`(params & 2**37) && !(params & 16)`) - see the section on the bit semantics
below. Bit 37 is a Matter-only opt-in. The library deliberately does not filter
on it and does not surface it.

A live join of the two catalogues on the reference install pins the bit's reach.
Every config row that the app-sync catalogue omits carries the bit. Rows that
app-sync still lists can carry it too, such as a hidden object or a phantom
stub, and the unfiltered params table serves the bit for those on both tiers. So
`not hidden` selects the same set on the admin tier that a restricted client
with a full grant sees.

`leaf_id` is not a visibility marker. Designer clears it when an object's Matter
box is unchecked, and the row keeps its type, its module, its rooms, and its
state. A re-check of the box writes `leafId` back from the linked leaf record. A
leafless object is a real object without leaf-derived facts. `leaf_key`,
`module_mac`, `sf_id`, `sub_sf_id`, and `leaf_io_no` read None, and
`is_server_owned` reads False. `record` stays None, because the description join
has no channel to match on. `AmpioClient.module_for()` still resolves the module
on the admin tier.

`is_system` (`typ_komponentu in {symulacja, detekcja}`) names the
presence-simulation and detection objects. They live outside the room tree, and
the app-sync catalogue lists them unconditionally. They carry no `leafId`. The
flag does not enter `visible`, so a hidden system object stays hidden.

Treat `visible` as the discovery filter.

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

Designer's per-object "time" field is the `czas` column. The wire unit is 10 ms
ticks. The library surfaces the value as `AmpioObject.pulse_ms`, in
milliseconds. The M-SERV never applies the value server-side: a plain `turnOn`
or `setValue` latches the object even when `czas` is set. Only an explicit time
argument pulses, and that argument is authoritative - `czas` neither stretches
nor caps it. Live measurements: with `czas` = 500 (5 s), a time argument of 100
ran 990 ms and an argument of 1000 ran 10011 ms. The timed form works on every
switchable output type probed (relay, flag, dimmer), independent of the bell
marker. The field is therefore the app's default pulse length - the app reads it
and sends the timed command itself, and a consumer honors it by passing the
value to `AmpioClient.set_value(pulse_ms=...)`. The column rides
`devicesDetails`, and the unfiltered `data/params_devices` table supplies it
where the app-sync catalogue omits it.

The M-SERV ships its own Matter bridge (a matter.js app launched by
`ampio-server`). That bridge's production gate corroborates the enum: it exposes
an object only when `(params & 2**37) && !(params & 16)`. Bit 37 is the
per-object Matter opt-in set in Designer. Bit 4 is the hidden/stub marker that
`hidden` and `visible` build on. The `leafId` structure
`0_<macHex>_<sfId>_<subSfId>_<ioNo>` that `AmpioObject.module_mac` parses is
likewise the structure the bridge's own classifier reads. The bridge also shows
why a dedicated integration is the right path for sensors. It types objects
through a registry with known gaps (no `lin_wej` branch, and loudness has no
Matter device type at all). And it exposes only the channels hand-flagged for
Matter - a dozen on the reference install, with humidity, pressure, illuminance,
and CO2 on zero modules.

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

## Deletion on the wire

Deletion behaves as follows on the wire, on the baseline server. A **module**
delete hard-removes its row from the `devices` list (the library evicts it and
dispatches `ModuleRemoved`), but it does not cascade to the module's objects. An
**object** delete in the Ampio app is two-stage: the object first moves to
"Ungrouped", and a second delete purges it. On the `config` catalogue the purge
is soft. The row stays, `leaf_id` intact, with the `params` hidden bit set, so
it drops out through `visible`. The app-sync surfaces (`data/devices`,
`data/params_devices`) hard-remove it, and that is what lets the restricted tier
evict for real. On the reference install the app-sync catalogue lists exactly
the objects with a room, plus the two system objects.

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
`record.location`, and the entry's own description string in `record.desc`. The
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

### The list request/reply pair

Request: the payload `0` to `device_api/to/list`. Reply: JSON on
`device_api/from/list`, `{devices: [...]}` with one entry per module, the
M-SERV's own row included. Each entry carries `macUser` (the override, the id
every leaf embeds), `macProd` (the factory id), `protocol`, `name` (base64), and
`descriptions`, base64 of the module's full description record.

The per-module pair serves the same blob for one module. Request: an empty
payload to `device_api/to/<machex>/get_data`. Reply: JSON on
`device_api/from/<MACHEX>/info`. Both macs are the factory id, lowercase hex on
the request and uppercase on the reply. A module with a Designer override
answers on `mac_global` only and stays silent on `mac`, the M-SERV included. On
the reference install the list blob and the `get_data` blob were identical for
every module right after a Designer edit, so the library reads the list.

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
dispatch on change. `record.location` is the mounting location and `record.desc`
the CAN-resident module name. A record without the frame, or with `outLoc` 0,
reads unassigned (None). The module answered, so None is authoritative. A module
the sweep did not cover keeps its previous value, exactly like the per-object
side. On the reference install the installer tagged wall devices this way (an
M-SENS and three M-DOT panels carry room names) and left the cabinet modules
untagged.

### The join rule

An object joins its entry through
`(DESC_TYPE_BY_KIND[typ_komponentu], leaf_io_no)` within the description record
of its own module (`AmpioObject.module_mac`). `leaf_io_no` is the last `leafId`
segment. It is live-proven as the out-no key over `funkcja` across the full
catalogue: `funkcja` under- or over-matches, dependent on the kind, and
`leaf_io_no` does not. `DESC_TYPE_BY_KIND` ships only live-proven pairs:
`przekaznik` -> 12 (OUTPUTS), `roleta_procenty` -> 26 (ROLLER), `roleta_lamelki`
-> 26 (ROLLER), `led` -> 16 (OUT_OC_U8), `rgbw` -> 34. A kind outside that table
(`bit32`, `flaga`, `lin_wej`, `satel_alarm`, `temp` among them) resolves no
location. On the full-catalogue probe, each of those landed on two or more
descTypes with a cleared majority at once. The join for such a kind is thus
ambiguous, not merely unproven by sample size.

### Sweep coverage

`resolve_records()` returns a `RecordSweep`. Its `records` map holds the join
result. Its `answered_macs` and `silent_macs` sets say which catalogued modules
the list reply covered. The two sets matter because `AmpioObject.record` reads
None in two different cases. A module in `answered_macs` is in the reply and
carries no entry for that output. A module in `silent_macs` is in the module
catalogue but missing from the reply, so its objects say nothing either way. The
M-SERV's own row is a device like any other in both sets.

One request returns every record. The `timeout` argument bounds each of the two
replies, the name table and the list, so the call ends within twice that. A
reply that never arrives raises `AmpioTimeoutError`.

### Tier gate

The whole `device_api` tree is admin-only, exactly like the raw tree. A
restricted account gets silence on both the subscribe and the request.
`AmpioClient.resolve_records()` raises `RuntimeError` with the tier in the
message, instead of a hang on a reply that never comes.
