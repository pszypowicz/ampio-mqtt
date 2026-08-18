# Account tiers

Every Ampio account reaches the same broker, but the M-SERV serves two
different surfaces depending on the account: the reserved **`admin` login
is the administrator**, and every app-created user is a standard account.
The app offers no administrator toggle for its users (it refuses to create
a user named `admin` altogether - both verified in the Designer web app),
and per-user app permissions do not move an account between tiers: a
standard account granted every permission in the app is still a standard
account.

The tier is the authenticated login name: the broker verifies the
username at CONNACK and the app cannot create another `admin`, so a
held session under that name IS the administrator. The library decides
everything on it at construction - `AmpioClient.access_tier` is a
constant, the subscription set and discovery requests are tier-shaped
from the first connect. The `info` reply's account id (`-1` for the
admin pseudo-user, the users-table row id for an app user -
`AmpioServerInfo.access_tier`) is the wire's own confirmation, which
`test_connection()` reports at validation time so a config flow can
reject an account whose tier will not support what the consumer needs
(e.g. `modules`/`mserv_id`, which the standard tier never receives).

## What each tier gets

| Capability                                    | Administrator | Standard user                         |
| --------------------------------------------- | ------------- | ------------------------------------- |
| Object catalogue with full metadata           | all objects   | objects granted in the app            |
| `params` bitfields (visibility, hidden flag)  | yes           | yes (the table is not grant-filtered) |
| Per-object live state                         | all objects   | granted objects                       |
| Rooms (`fetch_rooms`)                         | yes           | yes                                   |
| Server identity (`server_info`)               | yes           | yes                                   |
| Scenes (`fetch_scenes`, scene commands)       | yes           | yes                                   |
| `resources` / `icons` tables (`data` surface) | yes           | yes                                   |
| `logging` config table (`data` surface)       | yes           | yes (the table is not grant-filtered) |
| md5 change-detection tree                     | yes           | yes                                   |
| Commands                                      | all objects   | granted objects                       |
| **Module list** (`modules`, `mserv_id`)       | yes           | **no**                                |
| **Raw channel tree** (`ampio/from/#`)         | yes           | **no**                                |
| **Module diagnostics** (voltage, temperature) | yes           | **no**                                |
| **CAN write tree** (`ampio/to/#`)             | yes           | **no**                                |

The raw-tree denial is enforced in the SUBACK: a
standard account's subscriptions to the four `ampio/from/...` filters
come back with reason code 128 even over MQTT 3.1.1 (where stock
mosquitto would grant silently and just filter delivery). The library
records those verdicts in `ConnectionStats.subscribe_failures`, so a
diagnostics blob carries the broker's own statement of the account's
raw-tree access on every connect.

Two of the gaps are narrower than the table suggests. The `data/devices`
rows carry `id_urzadzenia`, so a standard account still learns the module
ids that own its granted objects - without names, macs, or models, but
enough to group entities by physical module. And the M-SERV's own
identity needs no module list at all: `server_info` is served fully on
both tiers, so a consumer can anchor its hub device on
`AmpioServerInfo.mac` instead of `mserv_id`.

Grants bound reads and object writes alike. A command for an object
outside a standard account's grant is dropped with no effect and no
reply, and no state for it reaches that account's namespace.

**Bus events are the exception.** Raising one is bounded by neither the
object grants nor the per-event rights the app displays - a standard
account raised an event it had no right to. Whatever logic the installer
bound to that event then runs with full authority, so an account can
reach objects it cannot command directly. A dedicated standard account
is a real boundary for direct object control; it is not a boundary
against anything reachable through Ampio's own event logic.

## The latency difference is on reads only

The M-SERV publishes every input twice: once on the raw channel tree as
the decoded CAN value, and again on the per-object topic after re-encoding
it (see [`raw-channel-bridge.md`](raw-channel-bridge.md)). The raw form
lands first, and only administrators receive it.

Measured on one flag object, command to the module's own raw report versus
the same change on the per-object topic:

| Path                          | Latency    |
| ----------------------------- | ---------- |
| Raw channel (admin only)      | 38-47 ms   |
| Per-object topic (both tiers) | 147-189 ms |

So a standard account sees input edges roughly **100-140 ms later**. The
library's raw-channel bridge closes that gap automatically on the admin
tier; on the standard tier it never fires and inputs arrive on the
per-object path.

**Writes are not affected.** The same flag driven through the `/api`
command surface and through the admin-only CAN tree reaches the device in
41 ms and 36 ms respectively - a difference inside the noise of individual
trials. The `/api` translation costs nothing measurable, so there is no
latency reason to prefer the CAN write path even when an admin account is
available.

## Choosing a tier

A standard account is the better default: it is least-privilege for reads
and writes, and it covers sensors, lights, switches, covers, and ordinary
input events with no functional gaps.

Prefer an administrator account when the install needs:

- **Sub-50 ms input reaction** - HA-side double-click, long-press, or
  hold-to-dim timing, where an extra ~130 ms is felt. Presses the M-SERV
  itself classifies arrive as ordinary objects and need no admin.
- **Module metadata** - per-module device entries, models, firmware
  versions, and `mserv_id` for a `via_device` hierarchy.
- **Bus events** - panel presses and other Ampio logic signals only
  arrive on the admin tier. A standard account can still raise events
  (see the exception above), so automation _into_ Ampio works either
  way; it is reacting _to_ Ampio's own events that needs admin.
- **Module health** - each module broadcasts its CAN supply voltage, and
  those with a temperature sensor their temperature, as
  `AmpioModule.supply_voltage` / `temperature`. Useful for spotting a
  sagging bus or a hot module before it misbehaves.
- **The CAN vocabulary** for device classes `/api` cannot express (CCT,
  DALI, display text); see [`untapped-surfaces.md`](untapped-surfaces.md).
