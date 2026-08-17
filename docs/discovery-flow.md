# Discovery flow

`AmpioClient.start()` runs the bring-up sequence: connect, subscribe,
publish the auto-discovery keywords, wait for the responses, return.
By the time `start()` returns - unless the `discovery_timeout`
elapsed first - `client.objects` and
`client.server_info` are populated and ready to consult
(`client.modules` too on the admin tier - see below). Live state
arrives via push from that point on.

A consumer that **depends** on those collections being populated before
it does anything else - the canonical case is resolving `mserv_id` to
pre-register the M-SERV device so other modules' `via_device` parents
resolve - should not rely on `start()`'s blocking as an implementation
detail. It should call `await client.wait_for_initial_discovery()`
(default `timeout=8.0`) explicitly. That method returns `True` once
discovery is complete for the account's tier and `False` if the timeout
elapses; it never raises. `start()` itself delegates its discovery wait
to this method and returns its result, so the two share one definition
of "discovery is done."
Expressing the dependency explicitly keeps `start()` free to return
earlier in a future revision without silently breaking that ordering -
the library's own accessors degrade gracefully when nothing is known
yet, so this guarantee exists for consumers, not for the library.

Authoritative source:
[`src/ampio_mqtt/_connection.py`](../src/ampio_mqtt/_connection.py) for the
session and reconnect loop,
[`src/ampio_mqtt/_store.py`](../src/ampio_mqtt/_store.py) for what a message
does to state, and [`src/ampio_mqtt/client.py`](../src/ampio_mqtt/client.py)
for the `start()` / `stop()` lifecycle that joins them.

## Sequence

1. **Connect** - TCP to the broker; authenticate; start the
   capped-exponential reconnect loop. The first successful connect
   stamps `stats.started_at`; each subsequent one bumps
   `stats.reconnect_count`.
2. **Subscribe** - the per-user topics (`ob/+/state`, the ten
   response topics) plus the global raw-channel wildcards, sent as one
   QoS 1 SUBSCRIBE packet. The SUBACK verdicts are read: a filter the
   broker rejects logs a warning and lands in
   `stats.subscribe_failures` while the connection stays up - on the
   baseline server a standard account is denied the four raw-tree
   filters this way. See [`protocol.md`](protocol.md) and
   [`raw-channel-bridge.md`](raw-channel-bridge.md) for the full
   subscribe list.
3. **Publish the auto-discovery keywords** on the matching control
   surfaces:
   - `devicesDetails` on `config` - object catalogue (admin tier).
   - `devices` on `config` - module catalogue (admin tier).
   - `devices` on `data` - app-sync object catalogue (every tier,
     grant-filtered).
   - `params_devices` on `data` - full-catalogue `params` table
     (every tier).
   - empty payload on `info` - server self-report.
   - empty payload on `states` - bulk snapshot of current values.
4. **Await** completion or the `discovery_timeout` deadline, whichever
   comes first - this step is `wait_for_initial_discovery()`, which
   `start()` calls with `timeout=discovery_timeout`. It first awaits
   `states` and `info`, reads the account tier off the info reply
   (`AmpioServerInfo.access_tier` - see
   [`account-tiers.md`](account-tiers.md)), then awaits that tier's
   catalogue pair: the `config` pair for the administrator, the `data`
   pair otherwise. A tier the info reply does not settle (a
   below-baseline server - see the README's supported versions) also
   waits on the `data` pair, which answers for every account. Each
   dispatched message bumps
   `stats.last_message_at`. The signals latch, so a later
   `wait_for_initial_discovery()` call returns immediately once its
   set has fired (and stays correct across reconnects).
5. **Return.** The library does not periodically refetch the
   catalogues; live state arrives via push on the per-object topic
   (and, for inputs, the raw-channel topics).

Every catalogue reply also evicts what it stopped listing (the admin
`config` catalogue and module list always; the app-sync `data/devices`
only on the restricted tier, where the grant bounds the store), fired
to consumers as `ObjectRemoved` / `ModuleRemoved` events. Since
catalogues are request/response,
a server-side deletion is noticed at the next reply - the refresh a
reconnect issues, or an explicit `refresh()`; a Designer module
deletion can commit without restarting the M-SERV, so a consumer that
wants prompt removals refreshes on its own schedule. An empty reply
never mass-evicts a populated store.

## What runs on demand, not automatically

Three helpers are not part of the auto sequence because the consumer
decides when - and whether - to call them:

- **`fetch_rooms()`** - the `groups` + `group_devices` join. The HA
  integration calls it once at setup to seed `DeviceInfo.suggested_area`.
  A non-HA consumer may not want it at all.
- **`fetch_scenes()`** - the scene catalogue, driven with
  `run_scene()` / `turn_scene_off()` / `undo_scene()`. Same rationale:
  a consumer that surfaces no scenes never pays for the fetch.
- **`fetch_locations()`** - the Designer "Location" name table. Same
  rationale; today it is consumed only for the diagnostics blob since
  the per-output pointer half is not on MQTT
  (see [`untapped-surfaces.md`](untapped-surfaces.md)).

All three helpers accept an explicit `timeout` (default 5.0 s) and raise
`AmpioTimeoutError` on timeout, so a flaky broker fails loud rather
than silently returning an empty result.

## Liveness counters

`client.stats` (a `ConnectionStats` dataclass) is what the HA
integration's diagnostics blob reads for connection health
(the blob also carries `last_payloads` and the location table).
The fields are:

- `reconnect_count` - monotonic; useful for a "works intermittently"
  report.
- `last_error` - the most recent `aiomqtt.MqttError` text, or `None`.
- `started_at` - epoch seconds of the first successful connect.
- `last_message_at` - epoch seconds of the most recently dispatched
  inbound message (any topic).
- `subscribe_failures` - filters the broker rejected in the latest
  connect's SUBACK, topic to reason code; empty when everything was
  granted.

These are intentionally cheap to read and cheap to update - on the
dispatch hot path, only `last_message_at` is touched.
