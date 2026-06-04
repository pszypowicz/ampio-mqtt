# Discovery flow

`AmpioClient.start()` runs the bring-up sequence: connect, subscribe,
publish the auto-discovery keywords, wait for the responses, return.
By the time `start()` returns, `client.objects`, `client.modules`, and
`client.server_info` are populated and ready to consult. Live state
arrives via push from that point on.

Authoritative source:
[`src/ampio_mqtt/client.py`](../src/ampio_mqtt/client.py) (`_run`,
`_dispatch`, the `start()` / `stop()` lifecycle).

## Sequence

1. **Connect** - TCP to the broker; authenticate; start the
   capped-exponential reconnect loop. Each successful (re)connect bumps
   `stats.reconnect_count` and stamps `stats.started_at` on the first
   one.
2. **Subscribe** - the per-user topics (`ob/+/state`, the seven
   response topics) plus the global raw-channel wildcards. See
   [`protocol.md`](protocol.md) and
   [`raw-channel-bridge.md`](raw-channel-bridge.md) for the full
   subscribe list.
3. **Publish the auto-discovery keywords** on the matching control
   surfaces:
   - `devicesDetails` on `config` - object catalogue.
   - `devices` on `config` - module catalogue.
   - empty payload on `info` - server self-report.
   - empty payload on `states` - bulk snapshot of current values.
4. **Await** all four responses or the `discovery_timeout` deadline,
   whichever comes first. Each dispatched message bumps
   `stats.last_message_at`.
5. **Return.** The library does not periodically refetch the
   catalogues; live state arrives via push on the per-object topic
   (and, for inputs, the raw-channel topics).

## What runs on demand, not automatically

Two helpers are not part of the auto sequence because the consumer
decides when - and whether - to call them:

- **`fetch_rooms()`** - the `groups` + `group_devices` join. The HA
  integration calls it once at setup to seed `DeviceInfo.suggested_area`.
  A non-HA consumer may not want it at all.
- **`fetch_locations()`** - the Designer "Location" name table. Same
  rationale; today it is consumed only for the diagnostics blob since
  the per-output pointer half is not on MQTT
  (see [`untapped-surfaces.md`](untapped-surfaces.md)).

Both helpers accept an explicit `timeout` (default 5.0 s) and raise
`AmpioConnectionError` on timeout, so a flaky broker fails loud rather
than silently returning an empty dict.

## Liveness counters

`client.stats` (a `ConnectionStats` dataclass) is the single source the
HA integration's diagnostics blob reads from. The four fields are:

- `reconnect_count` - monotonic; useful for a "works intermittently"
  report.
- `last_error` - the most recent `aiomqtt.MqttError` text, or `None`.
- `started_at` - epoch seconds of the first successful connect.
- `last_message_at` - epoch seconds of the most recently dispatched
  inbound message (any topic).

These are intentionally cheap to read and cheap to update - on the
dispatch hot path, only `last_message_at` is touched.
