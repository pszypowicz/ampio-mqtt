# Identity, replacement, and visibility

The single biggest gotcha for any consumer is picking the right id. The
M-SERV exposes several, with very different stability properties.
Replacement is the relevant axis: when a module is physically swapped
out, which fields the user sees stay the same and which don't.

Authoritative source: [`src/ampio_mqtt/models.py`](../src/ampio_mqtt/models.py).

## Modules

| Field                     | Stable across module replacement?                                                  | Use it for                                                                                           |
| ------------------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `id` (DB autoincrement)   | **No** - assigned in `mac_global` order; reassigned when a module is replaced.     | Cross-referencing objects to their owning module _within a single discovery snapshot_ only.          |
| `mac` (Designer override) | **Yes** - re-stamped onto the replacement unit so CAN logic elsewhere stays valid. | The replacement-stable per-module key. Also what the raw `ampio/from/<MAC>/...` topics are keyed by. |
| `mac_global` (factory id) | **No** - factory-burned, unique per physical unit, changes on swap.                | Display in diagnostics; never as identity.                                                           |

The M-SERV's default `mac` is `1`, which is not unique. Treat `mac` as
unique _within a single install_ (the user assigns the overrides), not
globally.

## Objects

| Field                     | Stable across module replacement?                                                                                                                                        | Notes |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| `id` / `device_id`        | **No** - DB autoincrement, change with the module.                                                                                                                       |
| `funkcja` (channel index) | **Yes** - part of the reloaded Designer config. Not unique: multiple objects can share one `funkcja` if the same physical signal is exposed as several Designer objects. |
| `typ_komponentu`          | **Yes** - the type vocabulary (`temp`, `lin_wej`, `flaga`, ...).                                                                                                         |
| `leaf_id`                 | **Yes**, when set. The wire-side visibility marker and the stable unique-id source - see below.                                                                          |

## Stable unique id: `leaf_id` (`AmpioObject.stable_key`)

The recommended per-object unique id is the Designer `leafId`, exposed
as `AmpioObject.stable_key` (`leaf_<leaf_id>`) and scoped per server by
the consumer:

```
{prefix}_leaf_{leaf_id}
```

`prefix` is a per-M-SERV scope: use `AmpioServerInfo.key`, the canonical
decimal form of the server's own CAN mac (served on every account tier,
and guaranteed present once `wait_for_initial_discovery()` returns
True). Why `leaf_id` rather than the module-mac composite
(`{prefix}_obj_{module.mac}_{typ_komponentu}_{funkcja}`):

- **Available on both account tiers.** The composite needs `module.mac`,
  which only administrator accounts receive (`config/devices` does not
  answer for standard accounts, and the app-sync catalogue carries no
  module list). `leafId` ships in both the `config` and app-sync `data`
  catalogues with identical values, so an install that
  upgrades a standard account to an administrator keeps every unique id.
- **Replacement-stable.** `leafId` is part of the reloaded Designer
  config, like `funkcja` and the `mac` override.
- **Unique among visible objects.** Across a full admin
  catalogue: no duplicate `leafId` once `visible` filtering is applied.
  The known collision - a hidden phantom stub sharing its labelled
  twin's `leaf_id` - is exactly what the `hidden` flag removes; filter
  on `visible` before keying. (Live installs can carry several such
  pairs at once, all on M-SENS analog channels.) #15 tracks the history.
  If a user deliberately exposes one physical signal as several visible
  Designer objects, those would share a `leafId` - and the same shape
  collides the composite too (same mac, typ, and funkcja), so
  `leaf_id` is never worse.

Objects with an empty `leaf_id` still need a fallback key: system
objects (`symulacja`, `detekcja`) are visible without one. On the admin
tier the module-mac composite above still covers them; on the standard
tier only the DB `id` is available, with its replacement instability
accepted and documented.

## Module identity on every tier: `AmpioObject.module_mac`

`leafId` embeds the owning module's override mac as its second segment
(`0_<macHex>_...`), exposed as `AmpioObject.module_mac`. The embedded
value equals `AmpioModule.mac` - live-verified across a full catalogue,
including the M-SERV, whose override (`1`) diverges from its factory
id - so a consumer can group entities by physical module even on the
restricted tier, which never receives the module catalogue. An entry
set up with a standard account and later switched to an administrator
keeps its entity-to-device mapping and only gains metadata. The parse
is strict: any shape other than `0_<macHex>_<F2>_<F3>_<F4>` reads as
None, exactly like the empty `leafId` of system objects and ghost rows.

## Visibility (`AmpioObject.visible`)

Not every row in `devicesDetails` is meant to be surfaced. The
predicate is:

```
visible = not hidden and (bool(leaf_id) or is_system)
```

- **`hidden`** - `params` bit 4 (`params & 16`). The M-SERV's own
  authoritative "do not surface" marker, and it takes precedence over
  everything else. It is set on phantom rows that duplicate a real
  Designer channel (same `leaf_id`, no value) and on objects the user
  hid - exactly the rows the `leaf_id` test alone wrongly keeps. It is a
  Designer config flag, so unlike the DB `id` it is replacement-stable.
  Every account tier receives `params` on a baseline server (via
  `devicesDetails` or `data/params_devices`); a row it has not arrived
  for yet reads as `0`, so `hidden` is False and the `leaf_id` test
  alone decides.
  This is the same gate the M-SERV's Matter bridge
  uses (`(params & 2**37) && !(params & 16)`) - see the section on the
  bit semantics below. Bit 37 is a Matter-only opt-in
  and is deliberately **not** used for filtering, so the library does not
  surface it.
- **`leaf_id`** - non-empty for every "real" object in the M-SERV's
  view. Empty for **ghost rows** (objects the user deleted in Designer
  but that still come back over the wire) and for **system objects**
  (presence simulation, detection). Use `leaf_id` non-empty as the
  default visibility filter.

- **`is_system`** - `typ_komponentu in {symulacja, detekcja}`. Always
  visible regardless of grouping; the M-SERV exposes these
  unconditionally.

Consumers should treat `visible` as the discovery filter. Ghosts that
slip in look like real entities until the user notices their HA
counterpart no longer exists in Designer.

## Where the `params` bit semantics come from

The M-SERV ships its own Matter bridge (a matter.js app launched by
`ampio-server`), and that bridge's production gate is the corroboration
for the two bits this library reads: it exposes an object only when
`(params & 2**37) && !(params & 16)` - bit 37 the per-object Matter
opt-in set in Designer, bit 4 the hidden/stub marker `hidden` /
`visible` build on. The `leafId` structure `0_<macHex>_<F2>_<F3>_<F4>`
that `AmpioObject.module_mac` parses is likewise the structure the
bridge's own classifier reads. The bridge also illustrates why a
dedicated integration is the right path for sensors rather than
leaning on it: it types objects through a registry with known gaps (no
`lin_wej` branch; loudness has no Matter device type at all), keys
endpoints on the volatile DB `id`, and exposes only the channels
hand-flagged for Matter - a dozen on the reference install, with
humidity, pressure, illuminance, and CO2 on zero modules.

How deletion behaves on the wire on the baseline server:
deleting a **module** hard-removes its row from the `devices` list (the
library evicts it and dispatches `ModuleRemoved`), but does not
cascade to its objects. Deleting an **object** in the Ampio app is
two-stage (it first moves to "Ungrouped", a second delete purges it)
and soft-deletes on the `config` catalogue: the row stays, `leaf_id`
intact, with the `params` hidden bit set - so it drops out through
`visible` - while the app-sync surfaces (`data/devices`,
`data/params_devices`) hard-remove it, which is what lets the
restricted tier evict for real.
