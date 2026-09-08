# Discovery flow

`AmpioClient.connect()` runs the bring-up sequence: connect, subscribe, publish
the auto-discovery keywords, wait for the responses, return. By the time
`connect()` returns - unless the `discovery_timeout` elapsed first -
`client.objects` and `client.server_info` are populated and ready to consult
(`client.modules` too on the admin tier - see below). Live state arrives via
push from that point on.

Some consumers **depend** on populated collections before they do anything else.
The canonical case resolves `mserv` to pre-register the M-SERV device, so other
modules' `via_device` parents resolve. Such a consumer must not rely on
`connect()`'s wait as an implementation detail. It must call
`await client.wait_for_initial_discovery()` (default `timeout=8.0`) explicitly.
That method returns `True` once discovery is complete for the account's tier,
and `False` if the timeout elapses. It never raises. `connect()` delegates its
discovery wait to this method and returns its result, so the two share one
definition of "discovery is done." The explicit dependency keeps `connect()`
free to return earlier in a future revision without a silent break of that
ordering. The library's own accessors degrade gracefully when nothing is known
yet, so this guarantee exists for consumers, not for the library.

Authoritative sources:
[`src/ampio_mqtt/_connection.py`](../src/ampio_mqtt/_connection.py) owns the
session and reconnect loop.
[`src/ampio_mqtt/_store.py`](../src/ampio_mqtt/_store.py) owns what a message
does to state. [`src/ampio_mqtt/client.py`](../src/ampio_mqtt/client.py) owns
the `connect()` / `disconnect()` lifecycle that joins them.

## Sequence

1. **Connect** - TCP to the broker, then authenticate, then start the
   capped-exponential reconnect loop. The run's first successful connect stamps
   `stats.started_at`. Each subsequent one bumps `stats.reconnect_count`.
2. **Subscribe** - the tier's topic set, sent as one QoS 1 SUBSCRIBE packet. The
   set is `ob/+/state`, the response topics of the tier's endpoints, and - on
   the `admin` login only - the retained `md5/devices` and `md5/params_devices`
   digests (see below) plus the global raw-channel wildcards. The digests lead
   the packet and the raw tree closes it. The broker replays retained values
   filter by filter and caps its outgoing QoS 1 queue at 1000 messages, and the
   raw tree alone exceeds that on a full install, so a filter listed after it
   loses its replay. It is decided at construction from the authenticated
   username (see [`account-tiers.md`](account-tiers.md)), so every filter must
   be granted. A SUBACK rejection lands in `stats.subscribe_failures` and warns,
   because it means a broken broker or ACL. See [`protocol.md`](protocol.md) and
   [`raw-channel-bridge.md`](raw-channel-bridge.md) for the topics.
3. **Publish the tier's auto-discovery keywords** on the matching control
   surfaces - four requests either way:
   - admin: `devicesDetails` and `devices` on `config` (object and module
     catalogues), plus `states` and `info`.
   - standard: `devices` and `params_devices` on `data` (grant-filtered app-sync
     catalogue and the full `params` table), plus `states` and `info`.

4. **Await** completion or the `discovery_timeout` deadline, whichever comes
   first. This step is `wait_for_initial_discovery()`, which `connect()` calls
   with `timeout=discovery_timeout`: one wait on the four replies of step 3.
   Each dispatched message bumps `stats.last_message_at`. The signals latch, so
   a later `wait_for_initial_discovery()` call returns immediately once its set
   fired (and stays correct across reconnects).
5. **Return.** The library does not refetch the catalogues on its own schedule.
   Live state arrives via push on the per-object topic (and, for inputs, the
   raw-channel topics). A Designer save reaches both tiers through the push
   described below. A consumer that wants a periodic catalogue re-read on top
   opts into `refresh_interval`.

Every catalogue reply also evicts what it stopped listing, fired as
`ObjectRemoved` / `ModuleRemoved`. The per-tier rules and the deletion-tool
differences live on the event docstrings and in [`identity.md`](identity.md).
Because catalogues are request/response, the next reply is what reveals a
server-side deletion. That reply comes from the Designer-save push, the refresh
a reconnect sends, an explicit `refresh()`, or a `refresh_interval` tick. An
empty reply is a complete reply that lists nothing, and it evicts like any
other.

### A Designer save

A Designer save rewrites the account tables on the M-SERV. A few seconds later
the M-SERV publishes three replies into every account namespace, the admin one
included, with no request from the account: `data/devices`, `md5/devices`, and
`data/params_devices`. Each tier learns of the save from a different one of
them.

- **Standard user.** The client subscribes to the two pushed tables as its
  catalogue pair, so it parses each push like a reply. The save surfaces at once
  as `ObjectAdded`, `ObjectUpdated`, or `ObjectRemoved`.
- **Administrator.** The M-SERV never pushes the `config` catalogues. The client
  subscribes to the retained `md5/devices` and `md5/params_devices` digests
  instead. The broker replays each retained digest after every subscribe, and
  that replay seeds the comparison, because the on-connect refresh already
  fetched the catalogues. A later digest that differs from the seed makes the
  client re-request `devicesDetails` and `devices`. The reply's diff then fires
  the same object events, and the module events with them. The re-request opens
  no snapshot cycle, so a value pushed since the last request keeps outranking
  the reply's `stan_json`.

The `md5/params_devices` digest covers the `params` table, which carries the
hidden bit. The admin catalogue carries that bit inline, so a rewrite of either
digest re-requests the same pair. A digest change that arrives while the
connection is down costs nothing extra: the reconnect refreshes the catalogues,
and the replay seeds again.

### Keeping the catalogue current without a reconnect: `refresh_interval`

`AmpioClient(..., refresh_interval=<seconds>)` opts into a periodic `refresh()`
while the connection is up. The default, `None`, leaves the cadence entirely to
the consumer. `connect()` schedules the periodic task and `disconnect()` cancels
it. A tick while the connection is down skips silently. The reconnect path
already refreshes on connect, so a periodic request adds nothing while the
broker is unreachable. Each cycle re-publishes the same initial-discovery
requests that `connect()` and `refresh()` send. The Designer-save push above
covers the common case on both tiers, so the tick is the fallback for a change
the M-SERV pushes no table or digest for. The next tick surfaces such a change
as `ObjectAdded` / `ObjectRemoved`, with no reconnect needed.

Each tick also runs `begin_refresh()`, which clears the live-value guard. An
undated live value can then be re-seeded from the M-SERV's DB snapshot on the
next reply. A raw-owned object is exempt, because its resync is the broker's
retained raw table, not the DB snapshot. Each cycle re-fetches the full
catalogue, so `refresh_interval` is sized in minutes, not seconds.

## What runs on demand, not automatically

Three helpers are not part of the auto sequence, because the consumer decides
when - and whether - to call them:

- **`fetch_rooms()`** - the `groups` + `group_devices` join. The HA integration
  calls it once at setup to seed `DeviceInfo.suggested_area`. A non-HA consumer
  can skip it.
- **`fetch_scenes()`** - the scene catalogue, driven with `run_scene()` /
  `off_scene()` / `undo_scene()`. Same rationale: a consumer that surfaces no
  scenes never pays for the fetch.
- **`resolve_records()`** - reads every module's record in one `device_api` list
  reply and folds the per-output Designer record into `AmpioObject.record`
  (admin tier only, see [`identity.md`](identity.md)). The per-module record
  folds into `AmpioModule.record`. A consumer that does not surface per-object
  records never pays for the read. The returned `RecordSweep` names the
  catalogued modules the reply covered and the modules it left out.

## Finding the M-SERV on the LAN

`discover()` resolves `ampio.local` with an explicit multicast DNS A-record
query driven by `python-zeroconf` (the `ampio-mqtt[discovery]` extra). Then it
TCP-probes the resolved address on the broker port. No service type or TXT
record on the LAN identifies that address as Ampio. That is why the lookup
targets the well-known hostname instead of a browse. See
[`lan-discovery.md`](lan-discovery.md) for the full probe facts, and for the
generic Matter records that share the address. Because the query runs inside the
process, it behaves the same on macOS, HAOS, plain Linux, and Docker, without
host-side `nss-mdns`/avahi configuration. A Home Assistant integration passes
its shared `AsyncZeroconf` via `discover(zeroconf=...)` instead of a second
multicast socket. The result is a hint based on the hostname alone. When
credentials are known, confirm identity with `check_connection()`.

## Liveness counters

`client.diagnostics_snapshot()` returns the one credential-free dict a
diagnostics platform emits as-is. It holds the connection counters, the SUBACK
rejections, the mac collisions, and each endpoint's verbatim last reply. The
`info` entry is the exception. Its reply carries the account's address,
coordinates, cloud endpoint, and public key, and a key-based redactor cannot
reach inside one retained string. The snapshot therefore masks every info value
outside a safe-key set and withholds an unparseable info reply. The counters are
cheap to update - the dispatch hot path touches only `last_message_at`.
