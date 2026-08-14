# Account tiers

Every Ampio account reaches the same broker, but the M-SERV serves two
different surfaces depending on the account's **administrator bit**. The
per-user app permissions do not move an account between tiers: a standard
account granted every permission in the app is still a standard account.

`AmpioClient.access_tier` reports which tier answered, settled by the time
`wait_for_initial_discovery()` returns. See
[`discovery-flow.md`](discovery-flow.md) for how it is detected.

## What each tier gets

| Capability                                   | Administrator | Standard user                         |
| -------------------------------------------- | ------------- | ------------------------------------- |
| Object catalogue with full metadata          | all objects   | objects granted in the app            |
| `params` bitfields (visibility, hidden flag) | yes           | yes (the table is not grant-filtered) |
| Per-object live state                        | all objects   | granted objects                       |
| Rooms (`fetch_rooms`)                        | yes           | yes                                   |
| Server identity (`server_info`)              | yes           | yes                                   |
| Commands                                     | all objects   | granted objects                       |
| **Module list** (`modules`, `mserv_id`)      | yes           | **no**                                |
| **Raw channel tree** (`ampio/from/#`)        | yes           | **no**                                |
| **CAN write tree** (`ampio/to/#`)            | yes           | **no**                                |
| Designer location table (`fetch_locations`)  | yes           | no                                    |

Grants bound reads and writes alike. A command for an object outside a
standard account's grant is dropped with no effect and no reply, and no
state for it reaches that account's namespace - so a dedicated standard
account is a real privilege boundary, not just a narrower view.

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
- **The CAN vocabulary** for device classes `/api` cannot express (CCT,
  DALI, display text); see [`untapped-surfaces.md`](untapped-surfaces.md).
