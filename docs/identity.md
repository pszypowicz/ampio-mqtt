# Identity, replacement, and visibility

The biggest trap for any consumer is the choice of id. The M-SERV exposes
several ids with very different stability properties. Replacement is the
relevant axis. When a module is physically swapped, which fields stay the same,
and which do not?

Authoritative source: [`src/ampio_mqtt/models.py`](../src/ampio_mqtt/models.py).

The rest of this area is on its own pages.

| Page                                               | Subject                                                                                                                    |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| [`visibility.md`](visibility.md)                   | The `visible` predicate, the `params` bit enum, the read-only and bell markers, and deletion on the wire.                  |
| [`description-records.md`](description-records.md) | The description record in each module: the Matter tag, the location pointer, the list reply, the join rule, and the sweep. |

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
devices), or `flush` (in-box `-p` modules). It reads None for virtual,
bridge-only, handheld, and unknown codes. The classification follows Ampio's
naming convention, which no official source asserts, so it is a hand-curated
table (`device_types.MODULE_MOUNTING`). Both fields decorate the HA device info
only. The HA device topology must never branch on them.

## Objects

| Field                     | Stable across module replacement?                                                                                                                                                                                                        | Notes                                                                                |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `id`                      | **Yes**. An object delete is soft on the `config` catalogue. The row stays, with the `params` hidden bit set, so the autoincrement never renumbers. Unchanged across years of configuration uploads, module replacements, and deletions. | The per-object unique id, exposed as `AmpioObject.object_key`.                       |
| `id_urzadzenia`           | **No** - it mirrors the module row, which is reassigned in `mac_global` order when a module is replaced.                                                                                                                                 | Cross-referencing an object to its module _within a single discovery snapshot_ only. |
| `funkcja` (channel index) | **Yes** - part of the reloaded Designer config. Not unique: if the same physical signal is exposed as several Designer objects, they share one `funkcja`.                                                                                |
| `typ_komponentu`          | **Yes** - the type vocabulary (`temp`, `lin_wej`, `flaga`, ...).                                                                                                                                                                         |
| `leaf_id`                 | **Yes**, when set. The physical-output key source and the parse source for `module_mac`. Empty for system objects and after a Matter uncheck - see below.                                                                                |

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
  surface. A standard account and an administrator account agree on it.
- **Stable.** Designer soft-deletes. The `params` hidden bit marks a removed
  object, and the row stays. The autoincrement therefore never has to renumber.
  Every id stayed unchanged across years of configuration uploads, module
  replacements, and deletions.

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
says nothing about visibility, which [`visibility.md`](visibility.md) covers.

One further collision exists and is unrelated to the Designer views above. A
hidden phantom stub can share its labeled twin's `leaf_id` on M-SENS analog
channels. The `hidden` flag removes exactly that stub, so filter on `visible`
before grouping by output.

## Module identity on every tier: `AmpioObject.module_mac`

`leafId` embeds the owning module's override mac as its second segment
(`0_<macHex>_...`), exposed as `AmpioObject.module_mac`. The embedded value
equals `AmpioModule.mac`, the M-SERV included, whose override (`1`) diverges
from its factory id. A consumer can thus group entities by physical module even
on the standard tier, which never receives the module catalogue. An entry
created with a standard account and later switched to an administrator keeps its
entity-to-device mapping and only gains metadata. The parse is strict: any shape
other than `0_<macHex>_<sfId>_<subSfId>_<ioNo>` reads as None, exactly like an
empty `leafId`.

Three helpers close the loop for a consumer that builds devices on `module_mac`.
`AmpioObject.is_server_owned` marks the objects that belong to the M-SERV itself
(their `leafId` embeds its override mac). They anchor to the hub device
identically on both tiers. `AmpioClient.mserv` returns the M-SERV's own module
row - name, model, versions - on the admin tier that has the catalogue. It is
the row whose `mac_global` or `mac` is the server's self-reported mac, and
nothing else in the list stands in for it. The override arm covers a replaced
unit, whose factory id changes while the re-stamped override does not.

`AmpioClient.module_for(obj)` resolves any object to its catalogue row. It joins
on `id_urzadzenia` and gates on mac agreement, so the volatile DB join can never
pair an object with a replaced module's stale row. The join keys the lookup
rather than the mac, because override macs can collide across rows. The mac then
gates what the join found. A leafless object has no mac to gate on, so its join
stands as is. On the reference install the join fails for the soft-deleted rows
alone: their `id_urzadzenia` points at a module the list no longer carries.

Both answer on the admin tier only, and raise on a standard account.

`AmpioObject.sibling_module_mac` is the module lookup that works on both tiers.
Every leafed object on the same `id_urzadzenia` embeds the module's override mac
in its leaf. The store reads that mac out of each catalogue reply for every row
that shares the module id. A leafless object thus names its module whenever one
leafed sibling is in the catalogue this tier holds. The field is separate from
`module_mac` on purpose. `module_mac` is the leaf-parsed fact, identical on both
tiers. `sibling_module_mac` depends on the grant, so the two can disagree
between tiers when the grant lacks a leafed sibling. The consumer picks which
one drives its topology. On the baseline install every module id maps to one
leaf mac, with no conflict on either tier.

## The leaf-id segments (`0_<macHex>_<sfId>_<subSfId>_<ioNo>`)

The Designer names all five segments. Its bundle builds the token, and it parses
the token back into `macGroup`, `mac`, `sfId`, `subSfId`, and `ioNo`. Earlier
revisions of this page called the last three `F2`, `F3`, and `F4`.

The library parses the mac, the `sfId`, the `subSfId`, and the trailing `ioNo`.
`AmpioObject.leaf_io_no` reads the last segment. It covers inputs as well as
outputs. `AmpioObject.sf_id` and `AmpioObject.sub_sf_id` read the third and
fourth segments, next to `module_mac` and `leaf_io_no`. A `subSfId` has meaning
only inside its `sfId`. Both read None when `leaf_id` is empty or malformed.

**`sfId` is a per-leaf special-function id, not the module type.** No module
showed `sfId` equal to its `typ_urzadzenia`. Virtual cover objects hosted on a
relay module carry the roller code, so the code follows the configured leaf
class, not the host product. The low codes match the Designer bundle's IO type
enum exactly where both are known:

| `sfId` | Leaf class                                                      |
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
meaning only in that scope. No global sub-function enum exists. The values
follow the same pattern the bundle uses:

| `sfId` | `subSfId` | Role                  |
| ------ | --------- | --------------------- |
| 257    | 1         | input (`wej`)         |
| 257    | 2         | output (`przekaznik`) |
| 296    | 3         | alarm armed           |
| 296    | 4         | alarm alarmed         |

Every single-role class uses 0. The bundle's own alarm special function names
sub-function 1 as input, 2 as output, 3 as armed, and 4 as alarmed. The rows
above show the same pattern.

`sfId` thus carries a tier-independent function-class signal (it rides the
app-sync catalogue the standard tier receives), but it cannot replace the module
type code. Both tables are coverage, not a specification, so an unlisted code
proves nothing. The library keeps its classification on `typ_komponentu` alone.
`sf_id` does not enter `kind`. The raw bridge reads it for `przekaznik` objects
only. A leaf of class 67 reports on the `a` prefix and takes the write byte
`0x32`. A leaf of class 257 reports on `o` and takes `0x30`. Any other class
reports on `o` and writes through `/api`. See
[`raw-channel-bridge.md`](raw-channel-bridge.md) and
[`panel-writes.md`](panel-writes.md).
