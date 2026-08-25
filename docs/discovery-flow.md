# Discovery flow

`AmpioClient.start()` runs the bring-up sequence: connect, subscribe,
publish the auto-discovery keywords, wait for the responses, return.
By the time `start()` returns - unless the `discovery_timeout`
elapsed first - `client.objects` and
`client.server_info` are populated and ready to consult
(`client.modules` too on the admin tier - see below). Live state
arrives via push from that point on.

A consumer that **depends** on those collections being populated before
it does anything else - the canonical case is resolving `mserv` to
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
   capped-exponential reconnect loop. The run's first successful connect
   stamps `stats.started_at`; each subsequent one bumps
   `stats.reconnect_count`.
2. **Subscribe** - the tier's topic set, sent as one QoS 1 SUBSCRIBE
   packet: `ob/+/state`, the response topics of the endpoints the tier
   uses, and - on the `admin` login only - the global raw-channel
   wildcards. The set is decided at construction from the authenticated
   username (see [`account-tiers.md`](account-tiers.md)), so every
   filter must be granted; a SUBACK rejection lands in
   `stats.subscribe_failures` and warns, because it means a broken
   broker or ACL. See [`protocol.md`](protocol.md) and
   [`raw-channel-bridge.md`](raw-channel-bridge.md) for the topics.
3. **Publish the tier's auto-discovery keywords** on the matching
   control surfaces - four requests either way:
   - admin: `devicesDetails` and `devices` on `config` (object and
     module catalogues), plus `states` and `info`.
   - standard: `devices` and `params_devices` on `data` (grant-filtered
     app-sync catalogue and the full `params` table), plus `states`
     and `info`.

4. **Await** completion or the `discovery_timeout` deadline, whichever
   comes first - this step is `wait_for_initial_discovery()`, which
   `start()` calls with `timeout=discovery_timeout`: one wait on the
   four replies of step 3. Each dispatched message bumps
   `stats.last_message_at`. The signals latch, so a later
   `wait_for_initial_discovery()` call returns immediately once its
   set has fired (and stays correct across reconnects).
5. **Return.** The library does not periodically refetch the
   catalogues; live state arrives via push on the per-object topic
   (and, for inputs, the raw-channel topics).

Every catalogue reply also evicts what it stopped listing, fired as
`ObjectRemoved` / `ModuleRemoved` - the per-tier rules and the
deletion-tool differences live on the event docstrings and in
[`identity.md`](identity.md). Since catalogues are request/response, a
server-side deletion is noticed at the next reply (the refresh a
reconnect issues, or an explicit `refresh()`), so a consumer that wants
prompt removals refreshes on its own schedule. An empty reply is a
complete reply listing nothing and evicts like any other.

## What runs on demand, not automatically

Two helpers are not part of the auto sequence because the consumer
decides when - and whether - to call them:

- **`fetch_rooms()`** - the `groups` + `group_devices` join. The HA
  integration calls it once at setup to seed `DeviceInfo.suggested_area`.
  A non-HA consumer may not want it at all.
- **`fetch_scenes()`** - the scene catalogue, driven with
  `run_scene()` / `turn_scene_off()` / `undo_scene()`. Same rationale:
  a consumer that surfaces no scenes never pays for the fetch.

## Finding the M-SERV on the LAN

`discover()` resolves `ampio.local` with an explicit multicast DNS
A-record query driven by `python-zeroconf` (the `ampio-mqtt[discovery]`
extra),
then TCP-probes the resolved address on the broker port. The M-SERV
publishes only its hostname over Avahi, with no service type and no TXT
records, which is why the lookup targets the well-known name. Because
the query runs inside the process, it behaves the same on macOS, HAOS,
plain Linux, and Docker, without host-side `nss-mdns`/avahi
configuration. A Home Assistant integration passes its shared
`AsyncZeroconf` via `discover(zeroconf=...)` instead of opening a second
multicast socket. The result is a hint based on the hostname alone;
confirm identity with `test_connection()` once credentials are known.

## Liveness counters

`client.diagnostics_snapshot()` returns the one credential-free dict a
diagnostics platform emits as-is: the connection counters, the SUBACK
rejections, the mac collisions, and each endpoint's verbatim last
reply. The counters are cheap to update - on the dispatch hot path,
only `last_message_at` is touched.
