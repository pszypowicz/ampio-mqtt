# Visibility and the params bits

This page continues [`identity.md`](identity.md) with the visibility predicate,
the `params` bit semantics, the read-only marker, and deletion on the wire.

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
account tier receives `params` on a baseline install, through `devicesDetails`
or `data/params_devices`. A row without a received value reads `0`, so it is
visible. This is the same gate the M-SERV's Matter bridge uses
(`(params & 2**37) && !(params & 16)`) - see the section on the bit semantics
below. Bit 37 is a Matter-only opt-in. The library deliberately does not filter
on it and does not surface it.

Every config row that the app-sync catalogue omits carries the bit. Rows that
app-sync still lists can carry it too, such as a hidden object or a phantom
stub. The unfiltered params table serves the bit for those on both tiers. So
`not hidden` selects the same set on the admin tier that a standard client with
a full grant sees.

`leaf_id` is not a visibility marker. Designer clears it when an object's Matter
box is unchecked, and the row keeps its type, its module, its rooms, and its
state. A re-check of the box writes `leafId` back from the linked leaf record. A
leafless object is a real object without leaf-derived facts. `leaf_key`,
`module_mac`, `sf_id`, `sub_sf_id`, and `leaf_io_no` read None, and
`is_server_owned` reads False. `sibling_module_mac` names its module when a
leafed sibling is in the catalogue. `AmpioClient.module_for()` resolves the
module row on the admin tier, and the record join falls back to `funkcja` (see
the join rule in [`description-records.md`](description-records.md)).

`is_system` (`typ_komponentu in {symulacja, detekcja}`) names the
presence-simulation and detection objects. They live outside the room tree, and
the app-sync catalogue lists them unconditionally. They carry no `leafId`. The
flag does not enter `visible`, so a hidden system object stays hidden.

Treat `visible` as the discovery filter.

## Where the `params` bit semantics come from

The Designer bundle embeds the enum that names every bit of the object `params`
integer:

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
`OPTION1` (bit 15) the label depends on the type:

- "Bell object" on `przekaznik` and `flaga`.
- "show switch in slider" on slider-shaped outputs.
- "1% lamella" on tilt covers.
- "block heating/cooling change" on `reg`.
- Other labels on camera, webview, and alarm objects.

A reader of an OPTION bit must gate on the component type first.

The library reads three of these bits. `DELETED` (bit 4) backs `hidden` and
`visible`. `READ_ONLY` (bit 6) backs `read_only`. `OPTION1` (bit 15) backs
`bell`, gated on the two component types the label applies to.

`MAKE_SEMICOLON` (bit 5) is Designer's "Divide by" checkbox. The library reads
neither the bit nor the divider (see [`classification.md`](classification.md)).

A bell object is meant for a single press. The Ampio app renders it as a
press-only button instead of a toggle. The checkbox is display intent: it sets
bit 15 and nothing else, and whether the output auto-releases is the module's
own configuration. The marker is readable on both account tiers, because
`data/params_devices` serves `params` unfiltered.

Designer's per-object "time" field is the `czas` column. The wire unit is 10 ms
ticks, and the library serves the raw column as `AmpioObject.czas`. The field's
meaning follows the component type. The Designer editor renders the column as
"turn-on time" on a fixed list of types. The list is `flaga`, `flaga_l`,
`flaga_p`, `przekaznik`, `led`, `flaga_liniowa`, `flaga_liniowa16`, `rgb`,
`rgbww`, and `ledww`. A camera reads the same column as a refresh time in
milliseconds. No other type gets the field, so a cover never carries a value.
The library exposes the turn-on time as `AmpioObject.pulse_ms`, in milliseconds,
on the listed types only. Every other type reads 0, whatever the column holds.

The M-SERV never applies the value server-side: a plain `turnOn` or `setValue`
latches the object even when `czas` is set. Only an explicit time argument
pulses, and that argument is authoritative - `czas` neither stretches nor caps
it. With `czas` = 500 (5 s), a time argument of 100 runs 990 ms and an argument
of 1000 runs 10011 ms. The timed form works on every switchable output type
(relay, flag, dimmer), independent of the bell marker. The field is therefore
the app's default pulse length. The app reads it and sends the timed command
itself. A consumer honors it by passing the value to
`AmpioClient.set_value(pulse_ms=...)`. The column rides `devicesDetails`, and
the unfiltered `data/params_devices` table supplies it where the app-sync
catalogue omits it.

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
Matter - a dozen on the baseline install, with humidity, pressure, illuminance,
and CO2 on zero modules.

## The read-only marker (`AmpioObject.read_only`)

Designer has a per-object "read only" checkbox. The checkbox sets `params` bit 6
and nothing else. The marker has these effects:

- The M-SERV enforces the marker itself, on both account tiers. An `/api` write
  to a read-only object produces no echo and no error. A watch on `hw/out`
  during the write shows why. The M-SERV emits zero CAN frames for the read-only
  object. The same write to a writable flag emits the normal frame set. Reads
  are unaffected on every surface.
- The marker never reaches the module. The description record is identical for a
  read-only flag and a writable one, so only the catalogue `params` field
  announces it.
- The standard tier can detect it. `data/params_devices` is served unfiltered,
  so `params` is available even for objects outside the grant.

The checkbox can change at any time in Designer. While `read_only` is True, a
consumer must reject writes and keep the entity's platform stable. In Home
Assistant, keep the entity a switch and raise an error on the service call. Do
not rebuild it as a binary sensor. A platform swap breaks the entity id, its
history, and every automation on each checkbox change.

## Deletion on the wire

Deletion behaves as follows on the wire, on the baseline install. A **module**
delete hard-removes its row from the `devices` list, and the library evicts it
and dispatches `ModuleRemoved`. The delete does not cascade to the module's
objects. An **object** delete in the Ampio app is two-stage: the object first
moves to "Ungrouped", and a second delete purges it. On the `config` catalogue
the purge is soft. The row stays, `leaf_id` intact, with the `params` hidden bit
set, so it drops out through `visible`. The app-sync surfaces (`data/devices`,
`data/params_devices`) hard-remove it, and that is what lets the standard tier
evict for real. On the baseline install the app-sync catalogue lists exactly the
objects with a room, plus the two system objects.
