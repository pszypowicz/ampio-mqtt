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
