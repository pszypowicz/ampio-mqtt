# Identity, replacement, and visibility

The single biggest gotcha for any consumer is picking the right id. The
M-SERV exposes several, with very different stability properties.
Replacement is the relevant axis: when a module is physically swapped
out, which fields the user sees stay the same and which don't.

Authoritative source: [`src/ampio_mqtt/models.py`](../src/ampio_mqtt/models.py).

## Modules

| Field | Stable across module replacement? | Use it for |
| --- | --- | --- |
| `id` (DB autoincrement) | **No** - assigned in `mac_global` order; reassigned when a module is replaced. | Cross-referencing objects to their owning module *within a single discovery snapshot* only. |
| `mac` (Designer override) | **Yes** - re-stamped onto the replacement unit so CAN logic elsewhere stays valid. | The replacement-stable per-module key. Also what the raw `ampio/from/<MAC>/...` topics are keyed by. |
| `mac_global` (factory id) | **No** - factory-burned, unique per physical unit, changes on swap. | Display in diagnostics; never as identity. |

The M-SERV's default `mac` is `1`, which is not unique. Treat `mac` as
unique *within a single install* (the user assigns the overrides), not
globally.

## Objects

| Field | Stable across module replacement? | Notes |
| --- | --- | --- |
| `id` / `device_id` | **No** - DB autoincrement, change with the module. |
| `funkcja` (channel index) | **Yes** - part of the reloaded Designer config. Not unique: multiple objects can share one `funkcja` if the same physical signal is exposed as several Designer objects. |
| `typ_komponentu` | **Yes** - the type vocabulary (`temp`, `lin_wej`, `flaga`, ...). |
| `leaf_id` | **Yes**, when set. The wire-side visibility marker - see below. |

A replacement-stable composite the HA integration uses for `unique_id`:

```
{prefix}_obj_{module.mac}_{typ_komponentu}_{funkcja}
```

`prefix` is the M-SERV's own CAN mac (from `AmpioServerInfo.mac`).
The composite is empirically collision-free for the entity surfaces
shipping today; #15 tracks the open question of whether the wire offers
a strict-unique stable per-object key.

## Visibility (`AmpioObject.visible`)

Not every row in `devicesDetails` is meant to be surfaced. The
predicate is:

```
visible = bool(leaf_id) or bool(group_ids) or is_system
```

- **`leaf_id`** - non-empty for every "real" object in the M-SERV's
  view. Empty for **ghost rows** (objects the user deleted in Designer
  but that still come back over the wire) and for **system objects**
  (presence simulation, detection). Use `leaf_id` non-empty as the
  default visibility filter.
- **`group_ids`** - parsed from `devicesDetails.powiazane` (GROUP_CONCAT
  of group memberships). Most M-SERV firmware leaves this empty; the
  membership lives in `data/group_devices` instead. Kept here for the
  rare firmware that does emit it.
- **`is_system`** - `typ_komponentu in {symulacja, detekcja}`. Always
  visible regardless of grouping; the M-SERV exposes these
  unconditionally.

Consumers should treat `visible` as the discovery filter. Ghosts that
slip in look like real entities until the user notices their HA
counterpart no longer exists in Designer.
