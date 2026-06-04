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
