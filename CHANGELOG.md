# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/) with one important
caveat documented in the [README](README.md): everything below `1.0.0` is
beta. Any 0.x.x bump is free to break consumers without a migration path
and without backwards-compatibility shims. `1.0.0` is reserved for the
release that accompanies the `home-assistant/core` integration PR being
accepted upstream.

The prior 1.x.x stream (`1.0.0` through `1.7.0`) was a development series
cut while the HA integration was taking shape; it has been retired in
favour of the explicit beta posture above and is no longer the supported
upgrade path.

## 0.16.0

Surfaces two failure modes a consumer could not previously see: a credential
rejection after startup (#53) and a server-info request that times out (#54).
Both were reported from the Home Assistant integration, where the first
blocks a reauthentication flow and the second produced a misleading "check
account permissions" message for what is in practice always a slow broker.

### Added

- `AmpioTimeoutError`, raised when the broker is reachable but an expected
  reply never arrives. It subclasses `AmpioConnectionError`, so handlers that
  treat every connection problem alike keep working.
- `AmpioClient.add_auth_failure_listener(listener)`: invoked with the
  broker's reason string when a reconnect attempt is rejected as unauthorized
  after `start()` has succeeded. By the time it fires the availability
  listeners have reported False and the connection loop has stopped for good,
  so it is the signal to drive reauthentication. A rejection during `start()`
  still raises `AmpioAuthError` there and does not invoke the listener.
- `AmpioClient.auth_failure`: the rejection reason once the loop has stopped
  because the broker rejected the credentials, `None` otherwise (including
  through outages the client is still retrying).

### Changed

- `AmpioClient.test_connection` raises `AmpioTimeoutError` when no info reply
  arrives within `info_timeout` instead of returning an empty
  `AmpioServerInfo`. A reply that arrives without identity fields is still
  returned as-is; live checks show every account tier gets full identity, so
  a missing reply had been the only source of empty results in practice.
- The on-demand fetches (`fetch_rooms`, `fetch_scenes`, `fetch_locations`)
  raise `AmpioTimeoutError` rather than the bare `AmpioConnectionError` when
  a reply does not arrive in time.

## 0.15.0

Splits the client into the two things it was doing. No behaviour change - the
same 366 tests and a live install produce the same objects, modules, rooms,
scenes and events as before.

### Changed

- `_connection.py` owns the MQTT session: the aiomqtt client, the subscribe
  set, reconnect with backoff, auth detection and availability. It knows
  nothing about what the messages mean.
- `_store.py` owns state: `AmpioStore.apply(topic, payload)` folds one message
  into the object, module and server state and returns an `Applied` describing
  what it touched - which endpoint replied, whether the payload parsed, and
  which objects, modules and events changed. No sockets, no tasks, no
  listeners, so every protocol behaviour is reachable from a plain function
  call.
- `client.py` keeps the public API and joins the two, turning what the store
  reports into listener callbacks and discovery latches.

The client drops from 1120 lines to 688. "Which objects changed" is now a
return value rather than something reconstructed from callbacks, and
`tests/test_store.py` exercises the protocol without a broker or an event loop.

## 0.14.0

Structural cleanup from a full review. No protocol behaviour changes; the model
gets smaller and the catalogue path stops doing the same work twice.

### Changed

- `classify()` returns one `ObjectKind` (`SensorKind | InputKind | OutputKind`)
  instead of a 3-tuple, and `AmpioObject.kind` holds it. A component type is a
  measurement, a boolean input, or something controllable, never two, so the
  three kinds are alternatives - the old shape could represent combinations
  that cannot occur, and three tests existed only to assert they never did.
  `is_sensor` / `is_input` / `is_output` / `supports_tilt` are now `isinstance`
  checks over the one field.
- The two catalogue handlers are one. Both discovery surfaces carry the same
  rows - the app-sync one simply omits `params` and `stan_json` - so a single
  merge covers them, replacing ~55 lines that expressed ~10 lines of difference.
- Catalogue merges only notify when something actually changed. Re-requesting
  both surfaces on every reconnect used to hand a consumer two full sets of
  updates saying nothing new; on a 50-object install that is 100 notifications
  down to 0, each one an `async_write_ha_state()` in Home Assistant.
- `AmpioObject.tilt_position` is `int | None`; a slat angle is always a percent.
- `mserv_id` resolves its fallback through `Capability.HUB` rather than a bare
  `typ_urzadzenia == 10` literal.
- Module diagnostics survive a catalogue refresh instead of being reset by it.

### Removed

- `AmpioObject.group_ids` and the `powiazane` parser. The column is empty on
  every observed firmware and the code said as much - room membership comes
  from `fetch_rooms()`. `visible` loses the clause it fed.
- `AmpioObject.matter_exposed`. Informational only, filtered on by nothing, and
  read by nothing; the bit stays documented in `docs/matter-bridge.md`.
- `AmpioState` from the public exports - the client's `objects` / `modules` /
  `server_info` / `sensors` properties are the read surface.
- An unreachable guard in the raw-channel handler, whose test had to delete an
  object by hand to reach it.

## 0.13.0

Keeps the connection alive through the things that used to end it silently.
Each fix below is a way the runner task could die, after which nothing
reconnected, every entity froze at its last value, and `stop()` re-raised the
failure so a consumer could not even tear the client down.

### Fixed

- A listener that raises no longer kills the connection. Listeners are consumer
  code; one failing is logged and the rest still run. Previously a single
  raising callback ended the client during the first discovery notify, before
  any value had been delivered.
- Replies of the wrong shape (`null`, a bare list, a non-list `List`, rows that
  are not objects) are rejected by the parsers instead of raising. The five
  catalogue parsers now share one guarded envelope helper, and the diagnostics
  frame validates its bytes - that one arrives off the CAN bus.
- `stop()` logs whatever the connection loop died of rather than re-raising it,
  so a consumer can always shut down, and it is safe to call twice.
- `start()` clears the connected latch, so restarting a stopped client waits
  for a real connection instead of returning immediately while offline.
- The reconnect backoff clamps its exponent. Attempts are unbounded and the
  float overflowed after ~1024 of them, killing the retry loop during a long
  outage.
- The bulk snapshot can now correct a value that changed while the connection
  was down. Per-object topics are not retained, so that snapshot is the only
  resync; it is compared against `AmpioObject.updated_at` rather than applied
  only when the value is unset, so it still loses to a newer live push.
- A catalogue reply carrying no `params` column no longer erases the flags
  `params_devices` supplied, which had exposed hidden phantom rows as entities.
  `ObjectMetadata.params` is `int | None` so an absent column is distinct from
  "no flags set".

### Added

- `AmpioObject.updated_at` - epoch seconds of the report a value came from.

## 0.12.0

Adds bus events, the logical 1-65535 signals Ampio's own logic raises and reacts
to, so a consumer can both drive Ampio scenarios and react to panel presses.

### Added

- `AmpioClient.send_event(number)` - raises an event via `/api/setEvent/<n>`.
- `AmpioClient.add_event_listener()` and `AmpioEvent(number, mac)` - the events
  the bus raises. The mac identifies the originator: the module's address for a
  panel press, the M-SERV's for an injected event.

### Notes

- The two directions are gated differently. Raising works on both tiers and is
  bounded by nothing: the per-event rights the Ampio app displays are not
  enforced on this surface, verified by a standard account raising an event it
  had no right to. Since the logic behind an event runs with full authority,
  this is how an account reaches objects it cannot command directly - the one
  hole in the otherwise grant-scoped standard tier. Receiving is
  administrator-only: it rides the raw tree, and a standard account holding the
  event's right still receives nothing anywhere.
- The M-SERV raises event 254 from its own mac whenever a client asks for a
  discovery refresh, so `start()` normally produces one. Filter on the
  originating mac to tell panel presses from the server's own signalling.

## 0.11.0

Exposes the Ampio app's scene catalogue and the three verbs that drive it,
closing #21.

### Added

- `AmpioClient.fetch_scenes() -> list[AmpioScene]` - the scene catalogue, with
  each scene's name, enabled flag, parent, and the objects its actions touch.
- `AmpioClient.run_scene()`, `turn_scene_off()`, and `undo_scene()`. The M-SERV
  replays the scene's own actions, so a consumer never sends them itself.
  `undo` restores the objects to the state they held before the scene ran,
  which is distinct from `off` driving them to zero - both verified live.
- `AmpioScene`, exported from the package.

### Notes

- Scene commands are grant-scoped like any other command: a scene touching
  objects outside a standard account's grant does nothing.
- The catalogue itself reaches both tiers.

## 0.10.0

Surfaces the health each module broadcasts about itself, for a per-module
diagnostics view in a consumer.

### Added

- `AmpioModule.supply_voltage` and `AmpioModule.temperature`, decoded from the
  module's `ampio/from/<MAC>/b/4F` broadcast. Voltage is the CAN bus supply;
  temperature is reported only by the modules that measure it and stays None
  elsewhere. Each frame also refreshes `last_seen`, so a module with no objects
  of its own still shows liveness.
- `AmpioClient.add_module_listener()` - fires when a module's own report
  updates it, mirroring `add_object_listener()`.

### Notes

- Administrator-only, like the rest of the raw tree: a standard account is not
  served these broadcasts and both fields stay None.
- The broadcasts are periodic rather than retained, so the fields fill in over
  the first minute of a session rather than at connect.

## 0.9.0

Classifies controllable objects, so a consumer no longer needs its own
`typ_komponentu` table to pick a platform, and corrects a wrong security claim
made in 0.8.0.

### Added

- `OutputKind` and `AmpioObject.output_kind` / `is_output` / `supports_tilt`.
  `classify()` now returns `(sensor_kind, input_kind, output_kind)` and covers
  `przekaznik`, `led`, `rgbw`, `roleta`, `roleta_procenty`, and
  `roleta_lamelki`. Each kind flags the verbs the object answers (dimmable,
  color, cover, position, tilt). `roleta_lamelki` previously fell through to
  the generic value sensor, so a slats blind surfaced as a text sensor.
- `AmpioObject.tilt_position` from the `lammel` state field, which only
  tilt-capable covers emit. It is also a runtime signal that an object has
  slats, independent of `typ_komponentu`.
- `set_cover_tilt(object_id, lamella)` - move a blind's slats without touching
  its position. Both axes take the `101` "leave it alone" sentinel, so a
  position move, a tilt move, or both at once is always a single command.

### Fixed

- **Corrected: commands are grant-scoped.** 0.8.0 documented the opposite, from
  a test against an object that turned out to be inside the account's grant.
  Re-verified against two non-granted objects of different types: the command
  is dropped with no effect, while the same command from an administrator
  succeeds. A dedicated standard account is a real privilege boundary for
  writes as well as reads, so the README caveat is withdrawn.

## 0.8.0

Adds the write path (#39). Commands go to `ampio/control/<user>/api` as
`/api/set/<id>/<verb>[/<arg>...]`; the verb vocabulary is the M-SERV's own HTTP
control API, whose OpenAPI spec ships inside the M-SERV web app bundle. Both
account tiers can command, so this completes the standard-user path.

### Added

- `AmpioClient.command(object_id, verb, *args)` - the generic, type-agnostic
  write. Any verb the M-SERV accepts works through it.
- Typed helpers for the verbs verified against live hardware: `turn_on`,
  `turn_off`, `toggle`, `set_value` (with an optional `pulse_ms` auto-revert),
  `set_color` (RGBW), `open_cover`, `close_cover`, `set_cover_position` (with
  an optional lamella angle).
- `tools/set_object.py` - command one object and watch the resulting state.
- Command documentation in `docs/protocol.md`: the verb table, which entries
  are live-verified, and the CAN-tree alternative in `docs/untapped-surfaces.md`.

### Notes

- **Commands are not grant-scoped.** The per-user grant filters reads only; a
  non-admin account can command any object in the installation, including ones
  absent from its catalogue. Verified live. The README states this where the
  account tiers are described - it is the main caveat of the standard-user
  path.
- `setValue`'s second argument is an auto-revert timer in 10 ms units (a timed
  pulse), not a transition/fade time. `set_value(..., pulse_ms=...)` takes
  milliseconds and converts.
- `setColors` accepts four channel arguments; object state reports the colour
  back as one packed integer, `R | G<<8 | B<<16 | W<<24`.
- The `ampio/to/<mac>/...` CAN write tree is admin-only (verified live), so the
  library uses the `/api` surface, which works on both tiers.

## 0.7.1

No library changes. The 0.7.0 tag never reached PyPI: its snapshot of the
release workflow pinned a publish action whose bundled twine rejects the
`Metadata-Version: 2.5` that current hatchling emits, and the tag is
deletion-protected, so the 0.7.0 content ships as 0.7.1 with the fixed
workflow.

## 0.7.0

Makes non-admin accounts first-class. The M-SERV gates the `config` discovery
surface and the raw `ampio/from/#` channel tree on the account's administrator
bit; a standard user, whatever its app permissions, is served the app-sync
`data` surface instead, filtered to the objects the administrator granted it
in the app. The library now discovers through whichever surface answers,
reports which one did, and recommends a unique-id scheme that is identical on
both tiers. All wire behaviour was verified against a live M-SERV with an
administrator account and a fully-permissioned standard account side by side.

### Added

- `data_devices` / `params_devices` endpoints: the app-sync object catalogue
  (`data/devices` - the `devicesDetails` row shape minus `params` and
  `stan_json`, grant-filtered per account) and the full-catalogue `params`
  table (`data/params_devices` - not grant-filtered, which is what lets a
  standard account apply the hidden-flag visibility rule). Both join the
  initial-discovery set; on the admin tier they merge additively and never
  degrade the richer `devicesDetails` reply.
- `AmpioClient.access_tier` (`AccessTier.ADMIN` / `RESTRICTED` / `UNKNOWN`),
  detected from which surface answered. Settled by the time
  `wait_for_initial_discovery()` returns True; it can upgrade
  RESTRICTED -> ADMIN if a slow `config` reply lands later and never
  downgrades.
- `AmpioObject.stable_key` (`leaf_<leaf_id>`) - the recommended
  replacement-stable per-object unique id, identical on both tiers. The
  decision record, including why it replaces the module-mac composite, is in
  [docs/identity.md](docs/identity.md).

### Changed

- `wait_for_initial_discovery()` completes on the states snapshot and info
  plus either catalogue pair (`config` `devicesDetails`+`devices`, or `data`
  `devices`+`params_devices`) rather than requiring the `config` pair, so
  standard accounts now finish discovery instead of timing out. A new
  `admin_grace` parameter (default 2.0 s, spent from the same `timeout`
  budget) keeps waiting briefly for the `config` pair after the `data` pair
  completes, so `access_tier` is settled on return.
- README account requirements rewritten for the two tiers. The old text
  claimed a restricted account gets no server identity and no discovery;
  live verification shows it gets the full grant-filtered object catalogue,
  rooms, visibility flags, and `data/info` identity, and lacks the module
  list and the raw input topics.

## 0.6.0

Surfaces the `devicesDetails` `params` bitfield and uses its hidden flag as the
authoritative visibility marker, fixing the duplicated-Designer-channel case
where a phantom stub and a labelled object share one `leaf_id` (the downstream
unique-id collision tracked in #15, dup #32). The bit semantics are
reverse-engineered from the M-SERV's own Matter bridge; see
[docs/matter-bridge.md](docs/matter-bridge.md).

### Added

- `AmpioObject.params` - the raw `params` integer from `devicesDetails`.
- `AmpioObject.hidden` (`params` bit 4) - the M-SERV's "do not surface" marker,
  replacement-stable, set on phantom/stub rows and user-hidden objects.
- `AmpioObject.matter_exposed` (`params` bit 37) - the per-object Matter opt-in,
  informational only (never used for filtering).
- `docs/matter-bridge.md` - the reverse-engineered M-SERV Matter bridge: its
  `params` gate, `type`/`leafId` classification table, and gaps.

### Changed

- `AmpioObject.visible` now excludes `hidden` objects:
  `visible = not hidden and (bool(leaf_id) or bool(group_ids) or is_system)`.
  When `params` is absent (`0`) this degrades to the prior leaf_id heuristic.

## 0.5.0

A structural simplification with no behaviour change on the wire. The
request/response endpoints, formerly described in four parallel places
(per-endpoint topic builders, the subscribe block, the dispatch if/elif
chain, and a fan of per-endpoint events plus `last_*_payload` fields), are
now one `Endpoint` table that the client derives all four from. Object
classification, formerly five overlapping `typ_komponentu` sets plus two
`classify_*` functions, is now one `TYPE_PROFILES` table and one `classify()`.
Public API changes below are breaking - per the beta posture above, 0.x bumps
break freely.

### Changed

- `classify_object()` and `classify_input()` are replaced by a single
  `classify(typ, interpretacja) -> tuple[SensorKind | None, InputKind | None]`
  returning both classifications in one pass. `SensorKind` / `InputKind` are
  unchanged.
- The six `AmpioClient.last_*_payload` properties are replaced by one
  `AmpioClient.last_payloads: dict[str, str]` keyed by endpoint name
  (`details`, `devices`, `states`, `info`, `groups`, `group_devices`,
  `locations`). The states snapshot is now retained too.
- The four `request_details()` / `request_devices()` / `request_states()` /
  `request_info()` methods are replaced by `refresh()` (re-request the full
  initial-discovery set) and the general `request(name)`.

### Why

The four representations had to be edited in lockstep to add or change an
endpoint, and the five classification sets overlapped on the same key. Folding
each into a single table removes the synchronisation burden and makes adding an
endpoint or component type a one-line change, without altering any topic,
payload, or classification result.

## 0.4.0

### Added

- `AmpioClient.wait_for_initial_discovery(*, timeout=8.0) -> bool` - an
  explicit, opt-in way to block until the initial discovery cycle has
  populated `modules`, `objects`, and `server_info` (the four messages
  devicesDetails, devices, the states snapshot, and info). Returns `True`
  when all four have arrived, `False` if `timeout` elapses first. It never
  raises on timeout: restricted accounts may never receive the full set, in
  which case discovery continues opportunistically. The signals latch on
  first completion, so the call is reconnect-safe and returns immediately
  once discovery has happened.

### Changed

- `start()` now expresses its discovery wait by delegating to
  `wait_for_initial_discovery(timeout=discovery_timeout)`. No behaviour change
  for existing callers - `start()` still blocks on the initial cycle and still
  never raises on discovery timeout.

### Why

A consumer that must read `modules`/`objects`/`server_info` before building
on top of the client (e.g. resolving `mserv_id` to pre-register the M-SERV
device, so other modules' `via_device` parents resolve) previously relied on
an undocumented side effect: that `start()` happens to block until discovery
completes. The library itself does not need that guarantee - its accessors
degrade gracefully when nothing is known. Exposing the wait as its own method
lets that consumer depend on the contract explicitly, and frees `start()` to
return earlier in a future revision without silently breaking the ordering.

## 0.3.0

### Added

- `AmpioClient.fetch_locations() -> dict[int, str]` returning the
  Designer "Location" marker name table: the integer id -> human label
  the per-output dropdown in Designer is populated from (e.g.
  `{1: "Salon", 2: "Kuchnia", ...}`). Triggered by publishing
  `locations` on `ampio/control/<user>/config`; the broker replies on
  `ampio/fromDB/<user>/config/locations` with `{"List":[{"id",
"opis_menu"}]}`. The new topic is subscribed to on connect; the
  retained payload is exposed as `AmpioClient.last_locations_payload`
  alongside the other `last_*_payload` attributes.
- `LOCATIONS_REQUEST_PAYLOAD` constant and `locations_response_topic`
  topic helper.

### Why

The location is a per-output, user-editable string the integrator sets
in the Designer's "Location" column. The _name table_ (id -> label)
flows over MQTT and is what this method returns. The _per-output integer
pointer into that table_ lives on the module's CAN-resident description
table and is not published on any MQTT topic; resolving it would require
either an RPC bridge or a CAN sniff and is intentionally out of scope
here. Consumers that learn the per-output id by another route can use
this table to resolve it to a label; consumers that don't still get a
useful diagnostics blob (which locations does this M-SERV define?).

### Notes

- Additive; no breaking changes from 0.2.0.

## 0.2.0

### Added

- `AmpioClient.last_devices_payload`, `last_details_payload`,
  `last_info_payload`, `last_groups_payload`, `last_group_devices_payload`
  - the verbatim decoded MQTT payload as the broker sent it, retained per
    discovery topic so a downstream tester report can include the actual JSON
    the M-SERV emitted (instead of forcing the consumer to re-derive it).
    Pure passthrough, no parsing, no copies; replaces the previous private
    `_groups_payload` / `_group_devices_payload` attributes.
- `ConnectionStats` dataclass exposed as `client.stats`:
  `reconnect_count` (bumped on every successful `__aenter__` after the
  first), `last_error` (text of the most recent `aiomqtt.MqttError`),
  `started_at` (epoch seconds of the first successful connect),
  `last_message_at` (epoch seconds of the most recent dispatched message).
  Also exported from `ampio_mqtt`.

### Why

Surfaces what a downstream HA integration needs for a tester-facing
debug snapshot: the raw discovery JSON for repro-from-bug-report
workflows, and connection liveness counters so a "works intermittently"
report can be cross-checked against the actual reconnect count.

### Notes

- Additive; no breaking changes for consumers that used only the public
  API surface of 0.1.0. The two private payload attributes are renamed,
  which is fine in 0.x.x.
- Coverage stays at 98%, mypy `--strict` clean, ruff clean.

## 0.1.0

Initial beta cut consolidating the development surface into a single
beta entry. Capabilities the library exposes today:

- Async `AmpioClient` over the M-SERV MQTT broker with username/password
  auth, capped-exponential reconnect with jitter, and a graceful
  `start()` / `stop()` lifecycle.
- Discovery of physical modules and their logical DB objects from the
  M-SERV (`devices`, `devicesDetails`, `data/states`, `data/info`), with
  per-object live state pushed through `add_object_listener`.
- Low-latency raw-channel-to-object bridge for `state/i/<ch>` and
  `state/f/<ch>` so button-press / flag events land ahead of the
  per-object echo.
- `classify_object(typ, interpretacja) -> SensorKind | None` for the
  sensor side (temperature, humidity, pressure, illuminance, loudness,
  IAQ, CO2, generic linear inputs) and `classify_input(...) -> InputKind |
None` for the boolean/binary-sensor side (`flaga`, `detekcja`,
  `symulacja`).
- Per-module `Capability` flag set populated from the upstream Ampio
  devtypes catalogue (vendored as `_devtypes.json`): digital / analog /
  temperature inputs, env sensors, digital / roller / RGBW / IR outputs,
  plus role hints (UI panel, bridge, hub, alarm, AV).
- `AmpioObject.funkcja` (physical channel index), `is_sensor`,
  `is_input`, `is_on` (boolean interpretation of `value`).
- `AmpioObject.leaf_id` (the `leafId` token the M-SERV emits for every
  real object and leaves empty for ghost rows + system objects),
  `is_system` (typ in `SYSTEM_TYPES` = `{symulacja, detekcja}`), and
  `visible` = `bool(leaf_id) or bool(group_ids) or is_system`. The
  predicate consumers should use as their discovery filter so ghost rows
  the user no longer sees in Designer aren't surfaced.
- `AmpioClient.fetch_rooms()` returning `{object_id: room_name}` over MQTT
  from the `data/groups` + `data/group_devices` join; suitable for an HA
  integration to forward as `DeviceInfo.suggested_area` at first import.
- M-SERV identification (CAN mac, mac_global, firmware versions, local
  IP, device id) via `AmpioServerInfo`.
- Best-effort LAN discovery via `discover()` with optional shared
  `AsyncZeroconf`.

### Notes

- This is the first publicly visible 0.x.x release. The 1.x.x line on
  PyPI is being yanked and replaced by this stream.
- The strict-unique stable per-object key (the still-open half of #15)
  remains unresolved: `leaf_id` is not unique either - the same physical
  signal exposed as multiple Designer objects shares a `leafId`. Track
  via #15 if that ever becomes blocking; today the composite
  `{module.mac, typ_komponentu, funkcja}` plus the visibility filter is
  empirically collision-free for the sensor entities the HA integration
  surfaces.
