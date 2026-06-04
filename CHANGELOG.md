# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## 1.7.0

### Added

- `AmpioObject.group_ids: frozenset[int]` exposes the `grupy_obiektow`
  membership the M-SERV already reports in `devicesDetails.powiazane` (a
  comma-separated GROUP_CONCAT field the parser previously ignored). Empty
  for system objects (which have no room) and for ghost rows that survived
  removal from the Designer tree.
- `AmpioObject.is_system` and `AmpioObject.visible` derived properties.
  `is_system` is `True` for `typ_komponentu` in the new `SYSTEM_TYPES`
  constant (`symulacja`, `detekcja`). `visible` is
  `bool(group_ids) or is_system`, mirroring the M-SERV's own "visible
  objects" query so a downstream consumer (the Home Assistant integration)
  can drop the heuristic value/name filter and use the canonical predicate
  instead.

### Why

Closes the first half of #15. `devicesDetails` returns every row in the
M-SERV's object table - including objects the user has removed in
Designer (which only unassigns the row from all groups, never deletes
it). Without group membership a consumer cannot tell a removed-but-still-
returned ghost from a real object that happens to have a name and a cached
value. Surfacing `visible` lets the integration filter ghosts the way the
M-SERV's own UI does, which avoids the leak observed in Ampio's Matter
integration (a `przekaznik` row removed from the Designer tree still
exposed as a Matter device).

### Notes

- Purely additive; no breaking changes.
- `bit32` boolean inputs are still routed through `SensorKind`/`is_sensor`
  by `classify_object`; the new properties do not change classification.
- `leafId` (the strictly-unique stable per-object key tracked in the
  remaining half of #15) is unchanged - still parked pending a real
  hardware-swap test.

## 1.6.0

### Added

- `classify_input(typ, interpretacja)` and the `InputKind` dataclass, mirroring
  `classify_object`/`SensorKind` for input objects: `flaga` and `symulacja` map
  to a generic boolean (`device_class=None`), `detekcja` to `motion`. Exported
  from `ampio_mqtt`. `AmpioObject` gains `input_kind`, an `is_input` property,
  and an `is_on` property (truthy interpretation of `value`, so both the raw
  `"1"` and per-object `"255"` encodings read as on).
- Low-latency live state for input objects via a raw-channel bridge. The client
  now subscribes to the decoded per-channel topics for flags and digital inputs
  and routes each edge to the owning object, surfaced through the existing
  `add_object_listener()` pipeline. These raw channels fire on change and arrive
  ahead of the per-object republish (measured ~150 ms sooner), so button and
  flag events land with minimal latency. The raw stream is authoritative once
  seen for an object; the slower per-object echo is then suppressed to avoid a
  duplicate notification, while inputs never seen on the raw path keep their
  per-object updates.
- `AmpioObject.funkcja`, the object's physical channel index within its module.

### Changed

- Documented the identity semantics consumers need: `AmpioModule.mac` is the
  Designer-assignable bus address (replacement-stable), `mac_global` is the
  factory id (changes on hardware replacement), and `AmpioObject.id`/`device_id`
  are not stable across a hardware swap.

### Notes

- MQTT-only and additive; no new dependencies and no breaking changes.

## 1.5.0

### Added

- `AmpioModule.capabilities` is now populated for every known
  `typ_urzadzenia`. Returns a `frozenset[Capability]` (`StrEnum`) with up
  to 13 flags spanning physical I/O (`DIGITAL_OUTPUT`, `DIGITAL_INPUT`,
  `ANALOG_INPUT`, `TEMPERATURE_INPUT`, `ENV_SENSOR`, `ROLLER_OUTPUT`,
  `RGBW_OUTPUT`, `IR_OUTPUT`) and module-role hints (`UI_PANEL`,
  `BRIDGE`, `HUB`, `ALARM`, `AUDIO_VIDEO`). Many modules carry multiple
  capabilities - an M-OC-4s has `{DIGITAL_OUTPUT, ANALOG_INPUT,
RGBW_OUTPUT}`; an M-REL-8s has `{DIGITAL_OUTPUT, DIGITAL_INPUT,
TEMPERATURE_INPUT}`. A single label would discard most of the picture.
- Public helper `module_capabilities(type_code)` mirrors the shape of
  `module_model(type_code)` and returns `None` for unknown types so
  callers can distinguish "known module with no flags" from
  "unrecognised type code".
- The full upstream Ampio device-type catalogue (84 entries from
  `node-red-contrib-ampio/ampioin/db/devtypes.json`) is vendored as
  `src/ampio_mqtt/_devtypes.json` and loaded at import time via
  `importlib.resources`. `MODULE_MODELS` and `MODULE_CAPABILITIES` are
  built from it. Refreshing the catalogue is a one-file update.

### Why

The downstream Home Assistant integration needs to decide, per Ampio
module, which HA platforms to expose (switch / light / cover / sensor /
binary_sensor / climate / ...) and whether to bundle a module's child
objects into one HA device or split each into its own device linked via
`via_device`. The capability set is the honest abstraction:

- `DIGITAL_OUTPUT` -> module drives loads that may live in different rooms
- `RGBW_OUTPUT` -> light platform (with color)
- `ROLLER_OUTPUT` -> cover platform
- `DIGITAL_INPUT` -> button presses (future `binary_sensor` / `event`)
- `ENV_SENSOR` / `TEMPERATURE_INPUT` -> sensor platform
- `UI_PANEL` / `BRIDGE` / `HUB` -> structural hints, not HA platforms

The library exposes the _facts_; the integration applies the _policy_.

## 1.4.1

### Documentation

- Replaced the README's Usage example with an end-to-end snippet that
  exercises `discover()` and `AmpioClient.fetch_rooms()` alongside the
  basic listener loop. No library behaviour change; published so the
  PyPI project description reflects the 1.4 feature surface.

## 1.4.0

### Added

- `AmpioClient.fetch_rooms()` returns `{ampio_object_id: room_name}` by
  publishing the `groups` and `group_devices` keywords to
  `ampio/control/<user>/data` and joining the two responses. The M-SERV's
  MQTT-level `data/*` endpoints mirror its REST `/api/json/groups` and
  `/api/json/group_devices`, so no HTTP hop is needed.
- New const-module helpers `data_request_topic`, `groups_response_topic`,
  `group_devices_response_topic`, and the matching request payloads.
- New `ampio_mqtt.rooms.join_rooms()` pure helper exported for callers that
  want to join cached payloads themselves.

### Why

The HA integration backed by this library needs a per-device room hint so it
can pass `DeviceInfo.suggested_area` at first import (the pattern that
`lutron_caseta` and `niko_home_control` use; HA's dev blog confirms input-side
`suggested_area` is still officially supported, only the read-side
deprecation kicks in at HA Core 2026.9). The M-SERV's `obiekty.lokalizacja`
column we previously inspected is dead-code (uniformly `0` across installs);
the real linkage is the `grupy`/`grupy_obiektow` join, which the M-SERV
already exposes over MQTT under the same control/fromDB pattern used for
`devicesDetails`.

## 1.3.0

### Added

- `discover()` now drives multicast DNS resolution of `ampio.local` from
  inside the process via `python-zeroconf`, instead of relying on the OS
  resolver. This makes discovery behave the same on macOS, HAOS, plain
  Linux, and Docker containers - the previous code path silently failed
  anywhere `nss-mdns`/avahi was not configured on the host.
- `discover(zeroconf=...)` accepts an externally-managed `AsyncZeroconf`
  so a Home Assistant integration can share its existing instance instead
  of opening a competing multicast socket. When omitted, `discover()`
  creates a short-lived instance and closes it before returning.

### Changed

- `zeroconf>=0.131` becomes a required runtime dependency (it was an
  optional `[discovery]` extra in 1.1.0 and removed entirely in 1.2.0).
  Home Assistant always ships zeroconf, so this costs nothing in the
  primary deployment target.
- The library-internal mDNS A-record query replaces the previous
  `asyncio.open_connection("ampio.local", 1883)` + `getaddrinfo` flow.
  The TCP probe is still performed on the resolved IPv4 address as a
  final reachability check before returning a candidate. `DiscoveryResult`
  shape is unchanged.

## 1.2.0

### Removed

- `discover()` no longer browses `_mqtt._tcp.local.` via mDNS. The Ampio
  M-SERV does not publish itself on Avahi, so the mDNS path only surfaced
  unrelated brokers and added complexity. `discover()` is now hostname-only
  (a TCP probe of `ampio.local` by default).
- The `[discovery]` optional dependency (`zeroconf`) and the related
  `[[tool.mypy.overrides]]` block are gone.
- `DiscoveryResult` loses the `source` and `name` fields, and `discover()`
  loses the `include_mdns` parameter. These were only meaningful for the
  mDNS branch.

This is a breaking change versus 1.1.0 for callers that explicitly opted
into mDNS or read `DiscoveryResult.source`. The hostname-only behaviour
matches what every real install would have observed anyway, so no
functional regression is expected.

## 1.1.0

### Added

- `discover()` coroutine and `DiscoveryResult` dataclass: best-effort LAN
  discovery of an M-SERV by probing the `ampio.local` hostname and, when
  the new `[discovery]` extra is installed, browsing `_mqtt._tcp.local.`
  via zeroconf. Returns deduplicated candidates and never raises on
  "not found".
- New optional dependency extra: `pip install ampio-mqtt[discovery]`
  pulls in `zeroconf` for the mDNS strategy. Without it, `discover()`
  silently falls back to the hostname probe.

### Changed

- mypy: zeroconf is now declared as a scoped `ignore_missing_imports`
  override in `pyproject.toml` instead of per-line `# type: ignore`
  comments inside `discovery.py`. Strict typing stays clean whether or
  not the `[discovery]` extra is installed.

## 1.0.0

Initial public release.

### Added

- Async MQTT client for the Ampio M-SERV local DB-object protocol.
- Module and object discovery, bulk states snapshot, live per-object pushes.
- Server-info identification (mac, firmware versions, local IP).
- Sensor classification (`SensorKind`) with Home-Assistant-compatible
  device/state class hints for temperature and M-SENS environmental channels.
- Auto-reconnect with capped exponential backoff and jitter.
- Stable public API: `AmpioClient`, `AmpioError` and subclasses, `AmpioObject`,
  `AmpioModule`, `AmpioServerInfo`, `AmpioState`, `SensorKind`,
  `classify_object`, `module_model`.

### Known follow-ups

- The `aiomqtt` dependency is pinned to `>=2.0.0,<3`. When aiomqtt v3 lands,
  re-verify the auth-error heuristic in `_protocol._AUTH_ERROR_MARKERS` and
  bump the upper bound.
