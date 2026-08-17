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

Nothing on the wire enforces that uniqueness, so a misconfigured or
mid-commissioning install can deliver a catalogue where two modules share
a `mac`. `AmpioClient.colliding_macs` reports the affected values and a
warning naming the modules is logged when the set changes; a consumer
keying devices on `mac` should skip or disambiguate those modules rather
than merge them. While a `mac` collides the library routes no raw-channel
input events or diagnostics broadcasts for it - the sender is unknowable -
and affected inputs update through the per-object state path instead.

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

`prefix` is a per-M-SERV scope, e.g. the server's own CAN mac from
`AmpioServerInfo.mac` (which every account tier receives). Why `leaf_id`
rather than the module-mac composite
(`{prefix}_obj_{module.mac}_{typ_komponentu}_{funkcja}`):

- **Available on both account tiers.** The composite needs `module.mac`,
  which only administrator accounts receive (`config/devices` does not
  answer for standard accounts, and the app-sync catalogue carries no
  module list). `leafId` ships in both the `config` and app-sync `data`
  catalogues with identical values (live-verified), so an install that
  upgrades a standard account to an administrator keeps every unique id.
- **Replacement-stable.** `leafId` is part of the reloaded Designer
  config, like `funkcja` and the `mac` override.
- **Unique among visible objects.** Live-verified across a full admin
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
  When `params` is absent (older firmware) it is `0`, so `hidden` is
  False and the `leaf_id` test alone decides. Every account tier
  receives `params` (via `devicesDetails` or `data/params_devices`).
  This is the same gate the M-SERV's Matter bridge
  uses (`(params & 2**37) && !(params & 16)`); see
  [`matter-bridge.md`](matter-bridge.md). Bit 37 is a Matter-only opt-in
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
