# Account tiers

Every Ampio account reaches the same broker, but the M-SERV serves two different
surfaces. The reserved **`admin` login is the administrator**. Every app-created
user is a standard account. The app offers no administrator toggle for its
users, and it refuses to create a user named `admin`. Per-user app permissions
do not move an account between tiers. A standard account granted every
permission in the app is still a standard account.

The tier is a type. `AmpioClient` is the client every account gets, and
`AmpioAdminClient` extends it with what the M-SERV serves the reserved login
alone. The admin class carries its username, so no caller passes one. The base
class never inspects the username. The reserved login through the base class
gets the standard view, which is a valid least-privilege choice.

```python
client = AmpioClient(host, username, password)  # any account
admin = AmpioAdminClient(host, password)  # the reserved login
```

Neither class checks the account id the `info` reply reports.
`AmpioServerInfo.user_id` carries that id, and `AmpioServerInfo.access_tier`
reports the tier it implies. `check_connection()` reports that tier at
validation time. A config flow reads it to pick the class before any client
exists. The tier is an `AccessTier` member. `AccessTier.ADMIN` is the reserved
login and maps to `AmpioAdminClient`. `AccessTier.RESTRICTED` is the standard
account and maps to `AmpioClient`. Grouping entities by module needs no module
row on either class: `AmpioObject.address.mac` carries the key (see
[`identity.md`](identity.md)).

## What AmpioClient serves

| Member                                                                    | Wire source                                                           |
| ------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `connect()`, `disconnect()`, `wait_for_initial_discovery()`, `refresh()`  | the account's `control` and `fromDB` namespace                        |
| `check_connection()`, `available`, `diagnostics_snapshot()`               | the session                                                           |
| `objects`                                                                 | `data/devices`, `data/params_devices`, `data/states`, `ob/<id>/state` |
| `server_info`                                                             | `data/info`                                                           |
| `subscribe()`                                                             | the event stream                                                      |
| `fetch_rooms()`, `fetch_scenes()`                                         | `data/groups`, `data/group_devices`, `data/scenes`                    |
| `command()`, `turn_on()`, `turn_off()`, `switch()`, `set_value()`         | `/api` on the `control` topic                                         |
| `set_temperature()`, `set_heating_mode()`                                 | `/api`                                                                |
| `set_colors()`, `set_ww()`, `set_ww_power()`, `set_ww_coldness()`         | `/api`                                                                |
| `open()`, `close()`, `stop()`, `set_roller_pos()`, `set_roller_lamella()` | `/api`                                                                |
| `run_scene()`, `off_scene()`, `undo_scene()`, `set_event()`               | `/api`                                                                |
| `send_notification()`                                                     | `/api`                                                                |

## What AmpioAdminClient adds

| Member                                                                                | Wire source                              | Complete when                                    |
| ------------------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------ |
| `modules`, `mserv`, `module_for()`                                                    | `config/devices`                         | `wait_for_initial_discovery()` returns True      |
| `resolve_records()`                                                                   | `device_api/to/list`, `config/locations` | when the call returns                            |
| `records`, `cover_parameters`                                                         | the sweep, by object id                  | when `resolve_records()` returns                 |
| `module_records`, `capabilities`, `panel_settings`                                    | the sweep, by mac                        | when `resolve_records()` returns                 |
| `last_sweep`                                                                          | the sweep                                | when `resolve_records()` returns                 |
| `lock_target()`                                                                       | the sweep, `address` and `modules`       | when `resolve_records()` returns                 |
| `fetch_locations()`                                                                   | `config/locations`                       | when the call returns                            |
| `block_opening()`, `unblock_opening()`, `block_closing()`, `unblock_closing()`        | `ampio/to/<mac>/raw`                     | after a sweep filled the capability map          |
| `buzz()`, `buzz_pattern()`, `buzz_stop()`                                             | `ampio/to/<mac>/raw`                     | when `wait_for_initial_discovery()` returns True |
| `identify()`, `identify_stop()`                                                       | `ampio/to/<mac>/raw`                     | when `wait_for_initial_discovery()` returns True |
| `set_panel_backlight()`, `set_panel_status_light()`, `lock_panel()`, `unlock_panel()` | `ampio/to/<mac>/raw`                     | when `wait_for_initial_discovery()` returns True |

The admin client overrides `turn_on()`, `turn_off()`, `switch()` and the untimed
form of `set_value()` for one case. A binary or open-collector output on a CAN
module rides the raw write topic, because a panel's status LEDs ignore `/api` on
every account. Every other object, and every timed `set_value()`, takes the base
path. A test pins the table above to the code: a member without a row fails CI,
and a row without a member fails CI.

## What the admin session receives

| Surface                                                                | Wire source                                                                       | Complete when                |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ---------------------------- |
| raw-bridged state for inputs, panel LEDs, OC outputs, and CCT channels | `ampio/from/<mac>/state/*`, `ampio/from/<mac>/b/62`, `b/63`                       | after the retained replay    |
| `AmpioModule.supply_voltage`, `AmpioModule.temperature`                | `ampio/from/<mac>/b/4F`                                                           | after the first broadcast    |
| `AmpioModule.last_seen`                                                | any live message the module sends: a broadcast, a raw edge, or a per-object state | after the first live message |
| `ModuleUpdated`, `ModuleRemoved`                                       | the module catalogue and the raw tree                                             | on connect                   |
| `BusEventRaised`                                                       | `ampio/from/<mac>/event`                                                          | when Ampio logic raises one  |
| the `modules` and `mac_collisions` entries of `diagnostics_snapshot()` | the session                                                                       | always                       |

The SUBACK enforces the raw-tree denial. A standard account's subscription to
the `ampio/from/...` filters comes back with reason code 128. This holds even
over MQTT 3.1.1, where stock mosquitto grants silently and only filters
delivery. The library never runs into the denial, because a standard client does
not ask for the raw tree. But the verdict locks the table above to the broker's
own enforcement, not to convention.

### A standard account sees its own namespace and nothing else

The denial is not limited to the raw tree. A standard account subscribed to `#`
receives messages on two kinds of topic only: its own `ampio/fromDB/<user>/`
namespace, and the echo of its own publish on `ampio/control/<user>/api`.

An administrator subscribed to `#` at the same moment receives every other
account's `ampio/fromDB/<name>/` namespace as well, plus the raw tree, the
decoded CAN topics, the server heartbeat, the module status notifications and
the server log feed. The M-SERV fans the same object state into one namespace
per account.

So any surface published outside `ampio/fromDB/<user>/` is unreachable from a
standard account. Check that before you plan a consumer for one.

One of the gaps is narrower than the table suggests. The M-SERV's own identity
needs no module catalogue at all. Both tiers receive `server_info` fully, so a
consumer can anchor its hub device on `AmpioServerInfo.mac` instead of `mserv`.

Grants bound reads and object writes alike. The M-SERV drops a command for an
object outside a standard account's grant, with no effect and no reply. No state
for that object reaches the account's namespace. The drop is silent on the wire,
but the library can observe it. The `confirm=` option on the command methods
awaits the state echo and times out when none arrives. The timeout is how a
consumer tells a landed command from a discarded one (see the confirmation note
in [`commands.md`](commands.md)).

**Bus events are the exception.** Neither the object grants nor the per-event
rights in the app limit who can raise an event. The logic bound to an event runs
with full authority. A dedicated standard account is thus a real boundary for
direct object control only, and not against anything reachable through Ampio's
own event logic. The gating detail is in [`bus-events.md`](bus-events.md).

## One source per fact

The tier is fixed before the first connect, so every fact below has exactly one
source per tier. There is no precedence chain and no second opinion.

| Fact                                                                              | Source                                                                                                         |
| --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| object rows, names, leaf ids                                                      | `data/devices`                                                                                                 |
| `params`, `czas`, `url`                                                           | `data/params_devices`                                                                                          |
| the initial value of every object                                                 | `data/states`, except a raw-bridged object on `AmpioAdminClient`, whose initial value is the retained raw tree |
| module rows                                                                       | `config/devices`, admin only                                                                                   |
| `records`, `cover_parameters`, `module_records`, `capabilities`, `panel_settings` | the `device_api` sweep, on `AmpioAdminClient`                                                                  |

Both tiers hold the whole `params_devices` table, so every object either
catalogue lists has a row there. The two replies arrive in no fixed order, which
is why the library holds the table and applies it at the merge. The library
holds the catalogue until the table answers, so no object is served without its
`params`, `czas` and `url`. `wait_for_initial_discovery()` returning True is the
boundary: it waits for both replies of the tier's pair. If the table answers and
an object the catalogue lists has no row in it, the library warns. It lists that
object in the `params_gap` entry of `diagnostics_snapshot()`.

The model state is deterministic per tier. The tier is fixed at client
construction, the store starts empty, and nothing persists to disk. The class
fixes what a session can hold. `AmpioClient` has no `resolve_records()`, so no
admin fact can reach a standard session. If a consumer persists admin facts and
later runs as a standard account, that carry is the consumer's own choice.

## The latency difference is on reads only

The M-SERV publishes every input twice, and the raw form lands first (see
[`raw-channel-bridge.md`](raw-channel-bridge.md)). Only administrators receive
it.

Measured on one flag object, from the command to the module's own raw report,
and to the same change on the per-object topic:

| Path                          | Latency    |
| ----------------------------- | ---------- |
| Raw channel (admin only)      | 38-47 ms   |
| Per-object topic (both tiers) | 147-189 ms |

So a standard account sees input edges roughly **100-140 ms later**. The
library's raw-channel bridge closes that gap automatically on
`AmpioAdminClient`. On the standard tier the bridge never fires, and inputs
arrive on the per-object path.

**Write latency is not affected by the tier.** A flag write over `/api` echoes
in a median 40 ms. The one CAN route that carries a flag frame, `hw/out`, needs
six frames and echoes in a median 68 ms. See the flag entry in
[`designer-surfaces.md`](designer-surfaces.md). The library keeps `/api` for
flags on both tiers. On writes an admin account gains reach (the panel LEDs, the
panel colors, the touch lock, the buzzer, and the cover roller lock) and no
speed.

## Choosing a tier

A standard account is the better default. It is least-privilege for reads and
writes. It covers sensors, lights, switches, covers, and ordinary input events.
The cover roller lock and the panel status LED outputs are admin-only.

Prefer `AmpioAdminClient` when the install needs:

- **Sub-50 ms input reaction** - HA-side double-click, long-press, or
  hold-to-dim timing, where an extra ~130 ms is felt. Presses the M-SERV itself
  classifies arrive as ordinary objects and need no admin.
- **Module metadata** - per-module names, models, firmware versions, and `mserv`
  for a `via_device` hierarchy.
- **Bus events** - panel presses and other Ampio logic signals only arrive on
  the admin tier. A standard account can still raise events (see the exception
  above), so automation _into_ Ampio works on either tier. Only reactions _to_
  Ampio's own events need admin.
- **Module health** - most modules broadcast their CAN supply voltage, and those
  with a temperature sensor their temperature, as `AmpioModule.supply_voltage` /
  `temperature`. This is useful to find a sagging bus or a hot module before it
  misbehaves. The modules that send the frame are listed in
  [`raw-channel-bridge.md`](raw-channel-bridge.md).
- **Panel outputs, the panel buzzer, module identify, and the CAN vocabulary** -
  the raw write frames for panel status LEDs, the panel colors, the touch lock,
  the buzzer, the identify LED, and the cover roller lock. Also the device
  classes `/api` cannot express (DALI, display text). See
  [`panel-writes.md`](panel-writes.md) and
  [`untapped-surfaces.md`](untapped-surfaces.md).
- **The record sweep** for area assignment and module facts -
  `resolve_records()` fills `records`, `cover_parameters`, `module_records`,
  `capabilities` and `panel_settings`. It and `fetch_locations()` answer no
  other account.
