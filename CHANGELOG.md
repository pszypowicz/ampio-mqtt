# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

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
