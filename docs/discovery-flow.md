# Discovery flow

`AmpioClient.connect()` runs the bring-up sequence: connect, subscribe, publish
the auto-discovery keywords, wait for the responses, return. When `connect()`
returns, `client.objects` and `client.server_info` are populated and ready to
consult, unless the `discovery_timeout` elapsed first. On `AmpioAdminClient`,
`client.modules` is populated too (see below). Live state arrives via push from
that point on.

Some consumers **depend** on populated collections before they do anything else.
The canonical case, on `AmpioAdminClient`, resolves `mserv` to pre-register the
M-SERV device, so other modules' `via_device` parents resolve.
`wait_for_initial_discovery()` (default `timeout=8.0`) returns `True` once
discovery is complete for the client class, and `False` if the timeout elapses.
It never raises on timeout. It raises `AmpioNotConfigured` when the door refuses
a row (see [The door](#the-door)). `connect()` delegates its discovery wait to
this method and returns its result. Thus the two share one definition of
"discovery is done." A consumer checks the `connect()` result or awaits
`wait_for_initial_discovery()`.

Authoritative sources:
[`src/ampio_mqtt/_connection.py`](../src/ampio_mqtt/_connection.py) owns the
session and reconnect loop.
[`src/ampio_mqtt/_store.py`](../src/ampio_mqtt/_store.py) owns what a message
does to state. [`src/ampio_mqtt/client.py`](../src/ampio_mqtt/client.py) owns
the `connect()` / `disconnect()` lifecycle that joins them.

## Sequence

1. **Connect** - start the session loop. `connect()` waits up to `timeout` for
   the first pass that connects, authenticates and subscribes. A transport
   failure, on the first pass too, retries with capped exponential backoff. A
   credential rejection or an unexpected error stops the loop. The run's first
   successful connect stamps `started_at` in the `connection` entry of
   `diagnostics_snapshot()`. Each subsequent one bumps `reconnect_count`.
2. **Subscribe** - the tier's topic set, sent as one SUBSCRIBE packet. The set
   is `ob/+/state` and the response topics of the tier's endpoints.
   `AmpioAdminClient` adds the retained `md5/devices` and `md5/params_devices`
   digests (see below), the global raw-channel wildcards, and the
   `device_api/from/list` reply topic. Six raw state wildcards ask for QoS 0:
   the `f`, `i`, `o` and `a` state trees and the two CCT broadcasts. Every other
   filter asks for QoS 1. The retained replay then arrives whole (see
   [`raw-channel-bridge.md`](raw-channel-bridge.md)). The client class decides
   the set (see [`account-tiers.md`](account-tiers.md)), so every filter must be
   granted. A SUBACK rejection lands in `subscribe_failures` of the `connection`
   entry and warns, because it means a broken broker or ACL. See
   [`protocol.md`](protocol.md) and
   [`raw-channel-bridge.md`](raw-channel-bridge.md) for the topics.
3. **Publish the tier's auto-discovery keywords** on the matching control
   surfaces:
   - `AmpioAdminClient`: `devices` and `params_devices` on `data`, `devices` on
     `config`, plus `states` and `info`, five requests.
   - `AmpioClient`: `devices` and `params_devices` on `data`, plus `states` and
     `info`, four requests.

4. **Await** completion or the `discovery_timeout` deadline, whichever comes
   first. This step is `wait_for_initial_discovery()`, which `connect()` calls
   with `timeout=discovery_timeout`: one wait on the tier's replies of step 3.
   Each dispatched message bumps `last_message_at` in the `connection` entry.
   The signals latch, so a later `wait_for_initial_discovery()` call returns
   immediately once its set fired (and stays correct across reconnects).
5. **Return.** The library does not refetch the catalogues on its own schedule.
   Live state arrives via push on the per-object topic. On `AmpioAdminClient`,
   the raw tree also carries inputs, bridged `przekaznik` outputs and `ledww`
   CCT channels (see [`raw-channel-bridge.md`](raw-channel-bridge.md)). A
   Designer save reaches both tiers through the push described below. A consumer
   that wants a periodic catalogue re-read on top opts into `refresh_interval`.

Every catalogue reply also evicts what it stopped listing, fired as
`ObjectRemoved` / `ModuleRemoved`. The per-tier rules and the deletion-tool
differences live on the event docstrings and in
[`visibility.md`](visibility.md). Because catalogues are request/response, the
next reply is what reveals a server-side deletion. That reply comes from the
Designer-save push, the refresh a reconnect sends, an explicit `refresh()`, or a
`refresh_interval` tick. An empty reply is a complete reply that lists nothing,
and it evicts like any other.

### A Designer save

A Designer save rewrites the account tables on the M-SERV. A few seconds later
the M-SERV publishes three messages into every account namespace, the admin one
included. No account requested them: `data/devices`, `md5/devices`, and
`data/params_devices`. Designer triggers the push with a `refresh` keyword on
its `data` surface.

Both tiers subscribe to the two pushed tables as their catalogue pair. Each push
is parsed like a reply, and the save shows at once as `ObjectAdded`,
`ObjectUpdated`, or `ObjectRemoved`.

The M-SERV never pushes the module list. The admin client also subscribes to the
retained `md5/devices` and `md5/params_devices` digests, and a digest that
differs from its seed re-requests `config/devices`. The broker replays each
retained digest after every subscribe, and that replay seeds the comparison,
because the on-connect refresh already fetched the module list. The reply's diff
then fires the module events. The re-request opens no snapshot cycle, so a value
pushed since the last request keeps outranking the held snapshot seed.

The `md5/params_devices` digest covers the `params` table, which carries the
hidden bit. A rewrite of either digest re-requests the module list. A digest
change that arrives while the connection is down costs nothing extra: the
reconnect refreshes the catalogues, and the replay seeds again.

### Keeping the catalogue current without a reconnect: `refresh_interval`

`AmpioClient(..., refresh_interval=<seconds>)` opts into a periodic `refresh()`
while the connection is up. The default, `None`, leaves the cadence entirely to
the consumer. `connect()` schedules the periodic task and `disconnect()` cancels
it. A tick while the connection is down skips silently. The reconnect path
already refreshes on connect, so a periodic request adds nothing while the
broker is unreachable. Each cycle re-publishes the same initial-discovery
requests that `connect()` and `refresh()` send. The Designer-save push above
covers the common case on both tiers. The tick is the fallback for a change the
M-SERV pushes no table or digest for. The next tick reports such a change as
`ObjectAdded` / `ObjectRemoved`, with no reconnect needed.

Each tick also runs `begin_refresh()`, which clears the live-value guard. A
locally stamped live value can then be re-seeded from the M-SERV's DB snapshot
on the next reply. Only a raw channel edge leaves such a value, because the raw
tree carries no stamp of its own. A raw-owned object is exempt, because its
resync is the broker's retained raw state tree, not the DB snapshot. Each cycle
re-fetches the full catalogue, so `refresh_interval` is sized in minutes, not
seconds.

## The door

The store admits the object catalogue through one door, on both tiers. The door
waits for both replies of the pair, `data/devices` and `data/params_devices`,
because the hidden bit rides the second. Then it decides in one order. A row
with the hidden bit drops. Every remaining row must carry a leaf that parses
into `AmpioObject.address`. A row the M-SERV creates itself, `detekcja` or
`symulacja`, drops by its type before the door reads any leaf.

A row with an empty leaf stays out of `objects`. The store records it, and
`wait_for_initial_discovery()` raises `AmpioNotConfigured` with the `(id, name)`
pairs. The other rows are served and the connection stays up. After connect, the
same condition arrives as the `NotConfigured` event, and the row leaves through
`ObjectRemoved`. The next catalogue push that restores the leaf produces
`ObjectAdded`. `diagnostics_snapshot()` lists their ids under `not_configured`.

On `AmpioAdminClient`, the module list has its own door. The door admits no
module row whose override mac another row shares. The raw tree keys on that mac
and cannot attribute a frame to any of those rows. The store records the shared
mac and the module ids, and `wait_for_initial_discovery()` raises
`AmpioNotConfigured` with the pairs in `collisions`. After connect, the same
condition arrives as the `NotConfigured` event, and each row leaves through
`ModuleRemoved`. The next module list that gives each module its own mac reports
`ModuleUpdated`. The default mac `1` is not unique (see
[`identity.md`](identity.md)), so two rows left on it fail the door. The
installer gives each module its own mac in Designer. `diagnostics_snapshot()`
lists the pairs under `mac_collisions`.

A non-empty leaf that does not parse is a server fault. The store refuses the
reply whole as `AmpioProtocolError`, before any field changes. `data/devices`
carries the leaf, so `protocol_violations` names that topic whichever reply of
the pair ran the door.

A push of `data/params_devices` alone re-runs the door on the held catalogue. A
hidden bit that changes evicts or admits its row.

## Errors

Every error that the client classes raise subclasses `AmpioError`. There are two
exceptions. Access to `discover` or `DiscoveryResult` without the
`ampio-mqtt[discovery]` extra raises `ImportError`. The `ampio_mqtt.testing`
helper `apply_reply` raises `KeyError` or `RuntimeError`, as its docstring
states. `connect()` raises `AmpioAuthError` when the broker rejects the
credentials before the first successful connect. It raises
`AmpioConnectionError` when the broker is unreachable within `timeout`, or when
the connection loop stops during the connect. It raises `AmpioNotConfigured` as
`wait_for_initial_discovery()` does. A publish while the broker is disconnected
raises `AmpioConnectionError` too. A publish on the session raises
`AmpioTimeoutError` when the broker does not acknowledge it in time.
`check_connection()` publishes on its own probe session, and a timeout there
raises `AmpioConnectionError`. `check_connection()`, the fetch helpers,
`resolve_records()`, and a command with `confirm=` raise `AmpioTimeoutError`
when an expected reply does not arrive. `AmpioTimeoutError` subclasses
`AmpioConnectionError`, so a handler that treats every connection problem alike
keeps working. A rejection after a successful `connect()` arrives as the
`AuthFailed` event instead (see [`events.md`](events.md)).

Four classes separate whose fault a refusal is:

| Error                | Whose fault   | Raised for                                                                                                                                                                                |
| -------------------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AmpioValueError`    | the developer | A bad argument, a value beyond the frame's range, or an id the catalogue does not list. It also covers a call that needs a sweep that did not run. It subclasses `ValueError`.            |
| `AmpioNotConfigured` | the installer | A drivable row carries no leaf, or two or more module rows share one override mac. A lock write can also name a mac no admitted module row carries. The installer fixes each in Designer. |
| `AmpioUnsupported`   | nobody        | The install cannot do it. Examples are an output whose kind does not answer the verb, a kind no timed write pulses, and a module without the roller lock. A consumer omits the control.   |
| `AmpioProtocolError` | the server    | A reply lacks what its surface always serves.                                                                                                                                             |

## What runs on demand, not automatically

Four helpers are not part of the auto sequence, because the consumer decides
when - and whether - to call them:

- **`fetch_rooms()`** - the `groups` + `group_devices` join. The HA integration
  calls it once at setup to seed `DeviceInfo.suggested_area`. A non-HA consumer
  can skip it.
- **`fetch_scenes()`** - the scene catalogue (`AmpioScene` rows), driven with
  `run_scene()` / `off_scene()` / `undo_scene()`. Same rationale: a consumer
  that exposes no scenes never pays for the fetch.
- **`fetch_locations()`** - the Designer location name table, `AmpioAdminClient`
  only. `resolve_records()` fetches it itself, so a consumer that runs the sweep
  never calls it directly.
- **`resolve_records()`** - reads every module's description record in one
  `device_api` list reply, `AmpioAdminClient` only. One pass fills `records`,
  `cover_parameters`, `module_records`, `capabilities` and `panel_settings`, and
  returns a `RecordSweep` that reports which modules answered. The rule that
  reads the five datasets is in
  [`description-records.md`](description-records.md). A consumer that does not
  expose records never pays for the read.

## Finding the M-SERV on the LAN

`discover()` resolves `ampio.local` with an explicit multicast DNS A-record
query driven by `python-zeroconf` (the `ampio-mqtt[discovery]` extra). Then it
TCP-probes the resolved address on the broker port. The lookup targets the
well-known hostname because no LAN record identifies the M-SERV (see
[`lan-discovery.md`](lan-discovery.md)). Because the query runs inside the
process, it behaves the same on macOS, HAOS, plain Linux, and Docker, without
host-side `nss-mdns`/avahi configuration. A Home Assistant integration passes
its shared `AsyncZeroconf` via `discover(zeroconf=...)` instead of a second
multicast socket. It returns None when nothing answers. The result is a hint
based on the hostname alone. When credentials are known, confirm identity with
`check_connection()`.

## Liveness counters

`client.diagnostics_snapshot()` returns one report for a diagnostics platform or
a bug report. The library puts no password into it. It masks the account in
topics, the broker host in `last_error`, and the host identifiers of the server
info. The auth-failure reason names the reason code alone. A refused reply names
its column, and a refused `leafId` also names its row, without the value. The
retained info reply keeps the values that the parser accepts and leaves every
other key out. The error text of the MQTT stack in `last_error` is the one value
that passes through, with the account and the host masked. It holds the
availability flag, the auth-failure reason, and the safe server-info subset. It
also holds the connection counters, the SUBACK rejections, and each endpoint's
last reply summary. On `AmpioAdminClient` it also holds the mac collisions and
the module list. The `params_gap` entry names objects the params table skips.
The `not_configured` entry lists the ids of the rows the door left out. Their
Designer names stay out of the report, and the `NotConfigured` event carries
them.

The `modules` list holds one row per known module, sorted by id. Each row
carries the module's `id`, `mac`, `typ_urzadzenia`, `model`, `last_seen`,
`supply_voltage`, and `temperature`. A bug report about a quiet module reads its
`last_seen` from this list. The user-given module name stays out of the row. A
module that sends no `b/4F` frame keeps `last_seen`, `supply_voltage`, and
`temperature` empty after a connect. Its `last_seen` moves on the first push
from one of its objects. The modules that send the frame are listed in
[`raw-channel-bridge.md`](raw-channel-bridge.md).

The module list and the collision pairs write each mac as the string `0xCB8F`,
which is what `format_mac()` returns. The report is read by a person, and
[`identity.md`](identity.md) gives the rule. The mac in the server-info entry
stays a number. The decimal form of that number is the `server_key` a consumer
scopes its registry on. The entry is the `AmpioServerInfo` dataclass, with
`local_ip` and `device_id` masked as `**REDACTED**`. Those two fields identify
the host the M-SERV runs on.

Table replies retain a JSON string with only `row_count` in `last_payloads`.
Names, URLs, state descriptions, nested data, and unknown fields are omitted.
Malformed JSON or table envelopes retain `**REDACTED**`. A valid table envelope
retains its row count even if the endpoint parser refuses its rows. Discovery
and fetch methods still receive the full reply.

The `info` entry retains the parsed `mac` and `userId` under `Results`. It also
retains each version field in the dotted-number form, for example `1865` or
`3.4.5`. A version field in any other form reads `**REDACTED**`. Every other key
is left out, `Status` included. If the parser refuses the reply, the entry
retains `**REDACTED**`.

The `connection` entry carries six keys. `started_at` and `reconnect_count`
cover the current `connect()` run, so a deliberate restart never reads as a
flapping connection. `last_error` and `last_message_at` roll across runs.
`last_error` masks the account segment of any topic it names, and it masks the
broker host as `**REDACTED**`. The `subscribe_failures` key maps each topic the
latest SUBACK rejected to its reason code. `protocol_violations` maps each topic
whose reply the library refused to the reason, and rolls across runs too. Both
maps mask the account segment of the key, as in
`ampio/fromDB/<account>/ob/+/state`. The account names the surface no better
than the rest of the topic does. Also, a key-based redactor cannot reach a
credential that is itself a key. The global `ampio/from` tree carries no account
and keeps its whole topic.

## A reply the library refuses

Each surface serves a fixed column set, and the library refuses a reply that
drops one of them (see [`protocol.md`](protocol.md)). The refusal costs that one
message. The client logs it, records the masked topic and the reason in
`protocol_violations`, summarizes the reply in `last_payloads`, and holds the
connection up. Nothing else changes: the refused reply latches no discovery
signal, resolves no pending `fetch_*` call, and leaves held state untouched. So
a refused discovery reply makes `connect()` return False, exactly as silence
does, and `protocol_violations` is what tells the two apart.
