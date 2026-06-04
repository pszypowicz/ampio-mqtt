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
| `leaf_id`                 | **Yes**, when set. The wire-side visibility marker - see below.                                                                                                          |

A replacement-stable composite the HA integration uses for `unique_id`:

```
{prefix}_obj_{module.mac}_{typ_komponentu}_{funkcja}
```

`prefix` is the M-SERV's own CAN mac (from `AmpioServerInfo.mac`).
The composite is collision-free **once hidden objects are filtered out**
(see Visibility below). The one observed collision - the same physical
channel materialised as a phantom stub and a labelled object sharing one
`leaf_id` - is resolved because the phantom carries the `params` hidden
flag, so `visible` drops it. #15 tracks the history; the hidden flag is
the replacement-stable discriminator that closes it.

## Visibility (`AmpioObject.visible`)

Not every row in `devicesDetails` is meant to be surfaced. The
predicate is:

```
visible = not hidden and (bool(leaf_id) or bool(group_ids) or is_system)
```

- **`hidden`** - `params` bit 4 (`params & 16`). The M-SERV's own
  authoritative "do not surface" marker, and it takes precedence over
  everything else. It is set on phantom rows that duplicate a real
  Designer channel (same `leaf_id`, no value) and on objects the user
  hid - exactly the rows the `leaf_id` test alone wrongly keeps. It is a
  Designer config flag, so unlike the DB `id` it is replacement-stable.
  When `params` is absent (older firmware / restricted account) it is
  `0`, so `hidden` is False and the rule degrades to the former
  leaf_id heuristic. This is the same gate the M-SERV's Matter bridge
  uses (`(params & 2**37) && !(params & 16)`); see
  [`matter-bridge.md`](matter-bridge.md). Bit 37 (`matter_exposed`) is a
  Matter-only opt-in and is deliberately **not** used for filtering.
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
