# Description records

This page continues [`identity.md`](identity.md) with the record each module
holds for its outputs.

## The Matter device type tag (the `type` column)

The Designer "Description in device" panel lets the installer tag an output with
a Matter device type. Examples are "Lighting - On-off light" and "Plugs - Pump".
The tag lives in the module itself, as one per-output entry of the module's
description record: `{descType, outNo, outLoc, outType, desc}`. Designer writes
that record over `device_api/to/<macHex>/descriptions_wr` (base64 frames of
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
bundle embeds it:

| Group                 | Device types                                                                                                                                            |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Lighting              | 0x0100 On-off light, 0x0101 Dimmable light, 0x010C Color temperature light, 0x010D Extended color light                                                 |
| Plugs                 | 0x010A On-off plug-in unit, 0x010B Dimmable plug-in unit, 0x0303 Pump                                                                                   |
| Switches and controls | 0x0103 On-off light switch, 0x0104 Dimmer switch, 0x0105 Color dimmer switch, 0x0304 Pump controller, 0x000F Generic switch                             |
| Sensors               | 0x0015 Contact, 0x0106 Light, 0x0107 Occupancy, 0x0302 Temperature, 0x0305 Pressure, 0x0306 Flow, 0x0307 Humidity, 0x0850 On-off, 0x0076 Smoke/CO alarm |
| Closures              | 0x000A Door lock, 0x000B Door lock controller, 0x0202 Window covering, 0x0203 Window covering controller                                                |
| HVAC                  | 0x0300 Heating/cooling unit, 0x0301 Thermostat, 0x002B Fan, 0x002D Air purifier, 0x002C Air quality sensor                                              |

The description record in the module is authoritative for the tag. The `type`
column mirror lags it. An output tagged 256 (0x0100) in the description record
can still show an empty `type` column. `AmpioClient.resolve_records()` reads the
description record into `AmpioObject.record`, a `DesignerRecord`. The tag lands
in `record.matter_device_type`, the location pointer in `record.location`, and
the entry's own description string in `record.desc`. The column mirror stays in
`matter_device_type`, identical on both tiers. The two fields are separate
facts. The consumer picks which one to trust.

## The Designer location (per-output `outLoc`)

The Designer "Lokalizacja" dropdown sits on an output's "Description in device"
panel. It writes a second pointer into the same per-output entry as the Matter
tag above: `{descType, outNo, outLoc, outType, desc}`. `outLoc` indexes the
locations name table (request keyword `locations` on the admin `config` surface,
`{id, opis_menu, opis_rozwiniety}` rows), and 0 means unassigned. A read-back
needs the same description record the Matter tag lives in, because Designer does
not mirror `outLoc` to the object catalogue. The DB row's `lokalizacja` column
reads 0 for every object on the baseline install. `outType` differs: the `type`
column does mirror it, with the lag noted above.

### The list request/reply pair

The request and reply topics, the reply shape, and the per-module `get_data`
pair are in [`protocol.md`](protocol.md). The reply's `descriptions` field is
base64 of the module's full description record, and `macUser` is the override
every leaf embeds. The library reads the list reply, which carries every
module's blob in one message.

The blob decodes into repeated little-endian frames:

```
[len:2][descType:2][outNo:2][outLoc:2][outType:2][utf8 desc]
```

`len` counts the whole frame, header included. A frame whose `len` is below the
10-byte header ends the walk, and so does a frame that runs past the end of the
blob. The remainder is unreadable either way.

A cleared entry stays in the record. Designer rewrites the frame in place with
`outLoc` 16383, `outType` 0, and the placeholder description `.`. The frame
count never changes. The library reads all three sentinel values as absent.

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

### The module-level record (`AmpioModule.record`, a `ModuleRecord`)

The record's one DEVICE_NAME frame (descType 1) describes the module itself. Its
`desc` is the module name, and its `outLoc` is the module-level "Lokalizacja" -
where the module is mounted, not where its loads are. `resolve_records()` reads
it from the same reply and sets `AmpioModule.record`, with a `ModuleUpdated`
dispatch on change. `record.location` is the mounting location and `record.desc`
the module name from the description record. A record without the frame, or with
`outLoc` 0, reads unassigned (None). The module answered, so None is
authoritative. A module the sweep did not cover keeps its previous value,
exactly like the per-object side. On the baseline install the installer tagged
the wall devices this way, and left the cabinet modules untagged. An M-SENS and
three M-DOT panels carry room names.

### Module capabilities (`AmpioModule.capabilities`)

The same reply carries `supportedFunctions`, a base64 blob of 2-byte pairs. Each
pair is a function id and the number of channels the module has of that
function. `resolve_records()` decodes it and sets `AmpioModule.capabilities`,
with a `ModuleUpdated` dispatch on change. The sweep folds it together with the
module record, so a module that both change reports one event.

The mapping is keyed by the raw function id. The `ModuleFunction` enum names the
ids that a module on the baseline install advertises. An id without a member
still reads through under its number, so a module with a function this library
does not name loses nothing.

The value is a channel count, not a flag. On a touch panel the `BACKLIGHT_RGBW`
count is the number of touch fields:

| Module         | `BACKLIGHT_RGBW` count |
| -------------- | ---------------------- |
| 18-field M-DOT | 18                     |
| 9-field M-DOT  | 9                      |
| 4-field M-DOT  | 4                      |
| 2-field M-DOT  | 2                      |

On the baseline install the count matches the model name on all 12 panels. The
two facts are independent: the model name comes from the device type table, and
the count comes from the module itself.

A blob that is absent, not base64, or of odd length reads as an empty mapping,
and the device keeps its description record. Capabilities are additive, so an
unreadable capability blob must not cost the descriptions. A module the sweep
did not cover keeps its previous mapping. An empty mapping from a module that
answered is authoritative: it advertises nothing.

### The join rule

An object joins its entry through
`(DESC_TYPE_BY_KIND[typ_komponentu], leaf_io_no)` within the description record
of its own module (`AmpioObject.module_mac`). `leaf_io_no` is the last `leafId`
segment, and it is the Designer's own channel key. `DESC_TYPE_BY_KIND` ships
only these pairs:

- `przekaznik` -> 12 (OUTPUTS)
- `roleta_procenty` and `roleta_lamelki` -> 26 (ROLLER)
- `led` -> 16 (OUT_OC_U8)
- `rgbw` -> 34
- `flaga` -> 6 (FLAG_BIN)

A channel index repeats across classes by design, so a frame at the right index
in another class proves nothing on its own. The object name is the proof. Every
leafed flag on the baseline install has a class-6 frame at its channel. That
frame carries the object's own name wherever a name is set. A kind outside the
table (`bit32`, `lin_wej`, `satel_alarm`, `temp` among them) resolves no
location, because no class was proven for it.

A leafless object has no `leaf_io_no`. The join then uses the module that
`id_urzadzenia` resolves to and `funkcja` minus one as the channel. On the
baseline install `funkcja` minus one equals `leaf_io_no` for every leafed object
of every kind except `lin_wej`. The M-SENS analog channels follow another
numbering. The read is admin-only, so the module catalogue is present for the
join.

### Sweep coverage

`resolve_records()` returns a `RecordSweep`. Its `records` map holds the join
result. Its `answered_macs` set names every module the list reply listed, and
its `silent_macs` set names the catalogued modules the reply left out. The two
sets matter because `AmpioObject.record` reads None in two different cases. A
module in `answered_macs` is in the reply and carries no entry for that output.
A module in `silent_macs` is in the module catalogue but missing from the reply,
so its objects say nothing either way. The M-SERV's own row is a device like any
other in both sets.

One request returns every record. The `timeout` argument bounds each of the two
replies, the name table and the list, so the call ends within twice that. A
reply that never arrives raises `AmpioTimeoutError`.

### Tier gate

The whole `device_api` tree is admin-only, exactly like the raw tree. A standard
account gets silence on both the subscribe and the request.
`AmpioClient.resolve_records()` raises `RuntimeError` at once, instead of a hang
on a reply that never comes.
