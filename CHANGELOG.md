# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

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
