# Account tiers

Every Ampio account reaches the same broker, but the M-SERV serves two different
surfaces. The reserved **`admin` login is the administrator**. Every app-created
user is a standard account. The app offers no administrator toggle for its
users, and it refuses to create a user named `admin` (both verified in the
Designer web app). Per-user app permissions do not move an account between
tiers. A standard account granted every permission in the app is still a
standard account.

The tier is the authenticated login name. The broker verifies the username at
CONNACK, and the app cannot create another `admin`, so a held session under that
name IS the administrator. The library decides everything on it at construction:
`AmpioClient.access_tier` is a constant, and the subscription set and discovery
requests are tier-shaped from the first connect. The `info` reply's account id
is the wire's own confirmation (`-1` for the admin pseudo-user, the users-table
row id for an app user). `AmpioServerInfo.access_tier` carries it, and
`check_connection()` reports it at validation time. A config flow can then
reject an account whose tier will not support what the consumer needs. One
example is `modules`/`mserv`, which the standard tier never receives.

## What each tier gets

| Capability                                                                                   | Administrator | Standard user                         |
| -------------------------------------------------------------------------------------------- | ------------- | ------------------------------------- |
| Object catalogue with full metadata                                                          | all objects   | objects granted in the app            |
| `params` bitfields (visibility, hidden flag)                                                 | yes           | yes (the table is not grant-filtered) |
| Per-object live state                                                                        | all objects   | granted objects                       |
| Rooms (`fetch_rooms`)                                                                        | yes           | yes                                   |
| Server identity (`server_info`)                                                              | yes           | yes                                   |
| Scenes (`fetch_scenes`, scene commands)                                                      | yes           | yes                                   |
| `resources` / `icons` tables (`data` surface)                                                | yes           | yes                                   |
| `logging` config table (`data` surface)                                                      | yes           | yes (the table is not grant-filtered) |
| md5 change-detection tree                                                                    | yes           | yes                                   |
| Commands                                                                                     | all objects   | granted objects                       |
| Designer per-output record (the `device_api` tree, `resolve_records()`, `fetch_locations()`) | yes           | no                                    |
| Sibling module mac (`sibling_module_mac`)                                                    | yes           | yes, bounded by the grant             |
| **Module list** (`modules`, `mserv`)                                                         | yes           | **no**                                |
| **Raw channel tree** (`ampio/from/#`)                                                        | yes           | **no**                                |
| **Module diagnostics** (voltage, temperature)                                                | yes           | **no**                                |
| **CAN write tree** (`ampio/to/#`)                                                            | yes           | **no**                                |

The SUBACK enforces the raw-tree denial. A standard account's subscription to
the `ampio/from/...` filters comes back with reason code 128, even over MQTT
3.1.1 (where stock mosquitto grants silently and only filters delivery). The
library never runs into the denial, because a standard client does not ask for
the raw tree. But the verdict locks the table above to the broker's own
enforcement, not to convention.

Two of the gaps are narrower than the table suggests. The `data/devices` rows
carry `id_urzadzenia`, so a standard account still learns the module ids that
own its granted objects. That is enough to group entities by physical module,
but without names, macs, or models. `AmpioObject.sibling_module_mac` turns that
id into the module's override mac whenever one leafed object on the same module
is in the grant. And the M-SERV's own identity needs no module list at all. Both
tiers receive `server_info` fully, so a consumer can anchor its hub device on
`AmpioServerInfo.mac` instead of `mserv`.

Grants bound reads and object writes alike. The M-SERV drops a command for an
object outside a standard account's grant, with no effect and no reply. No state
for that object reaches the account's namespace. The drop is silent on the wire,
but the library can observe it. The `confirm=` option on the command methods
awaits the state echo and times out when none arrives. The timeout is how a
consumer tells a landed command from a discarded one (see the confirmation note
in [`protocol.md`](protocol.md)).

**Bus events are the exception.** Neither the object grants nor the per-event
rights in the app limit who can raise an event. The logic bound to an event runs
with full authority. A dedicated standard account is thus a real boundary for
direct object control only, and not against anything reachable through Ampio's
own event logic. The gating detail is in [`protocol.md`](protocol.md).

## How the model marks the tiers

The model separates facts by source. A plain field holds a fact both tiers
receive, and nothing changes it after the catalogue seed. Facts from the CAN
description record live in the nested `record` bundle: `AmpioObject.record` and
`AmpioModule.record`. The nesting is the marker. Everything under `.record`
needs the admin tier, and the bundle stays `None` on a standard account. The
library adds no precedence helper. When the two sources disagree, the consumer
picks.

The admin-fed fields, the nested bundles included:

| Field                                        | Why it is admin-only                  |
| -------------------------------------------- | ------------------------------------- |
| `AmpioObject.record`, `AmpioModule.record`   | filled by the `device_api` sweep only |
| `AmpioObject.raw_owned`                      | proven by the raw channel tree        |
| `AmpioModule.supply_voltage`, `.temperature` | module diagnostics broadcasts         |
| every `AmpioModule` row                      | the module list itself is admin-only  |

The model state is deterministic per tier. The tier is fixed at client
construction, the store starts empty, and nothing persists to disk. A restricted
client refuses `resolve_records()` before any wire traffic, so no admin fact can
appear on that tier. If a consumer persists admin facts and later runs
restricted, that carry is the consumer's own choice.

## The latency difference is on reads only

The M-SERV publishes every input twice. The raw channel tree gets the decoded
CAN value first, and the per-object topic gets the re-encoded form (see
[`raw-channel-bridge.md`](raw-channel-bridge.md)). The raw form lands first, and
only administrators receive it.

Measured on one flag object, from the command to the module's own raw report,
and to the same change on the per-object topic:

| Path                          | Latency    |
| ----------------------------- | ---------- |
| Raw channel (admin only)      | 38-47 ms   |
| Per-object topic (both tiers) | 147-189 ms |

So a standard account sees input edges roughly **100-140 ms later**. The
library's raw-channel bridge closes that gap automatically on the admin tier. On
the standard tier the bridge never fires, and inputs arrive on the per-object
path.

**Writes are not affected.** The same flag reaches the device in 41 ms through
the `/api` command surface and in 36 ms through the admin-only CAN tree. That
difference is inside the noise of individual trials. The `/api` translation
costs nothing measurable, so there is no latency reason to prefer the CAN write
path, even with an admin account available.

## Choosing a tier

A standard account is the better default. It is least-privilege for reads and
writes. It covers sensors, lights, switches, covers, and ordinary input events
with no functional gaps.

Prefer an administrator account when the install needs:

- **Sub-50 ms input reaction** - HA-side double-click, long-press, or
  hold-to-dim timing, where an extra ~130 ms is felt. Presses the M-SERV itself
  classifies arrive as ordinary objects and need no admin.
- **Module metadata** - per-module device entries, models, firmware versions,
  and `mserv` for a `via_device` hierarchy.
- **Bus events** - panel presses and other Ampio logic signals only arrive on
  the admin tier. A standard account can still raise events (see the exception
  above), so automation _into_ Ampio works on either tier. Only reactions _to_
  Ampio's own events need admin.
- **Module health** - each module broadcasts its CAN supply voltage, and those
  with a temperature sensor their temperature, as `AmpioModule.supply_voltage` /
  `temperature`. This is useful to find a sagging bus or a hot module before it
  misbehaves.
- **Panel outputs and the CAN vocabulary** - the raw write frame for panel
  status LEDs, and the device classes `/api` cannot express (CCT, DALI, display
  text). See [`protocol.md`](protocol.md) and
  [`untapped-surfaces.md`](untapped-surfaces.md).
- **Per-object Designer records** for area assignment - `resolve_records()` and
  `fetch_locations()` answer no other account.
