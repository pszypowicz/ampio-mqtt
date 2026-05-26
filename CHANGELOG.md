# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

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

Initial public release. The project was renamed from an unpublished
pre-release to avoid a PyPI name collision; this is the first release of the
codebase under the `ampio-mqtt` distribution name.

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
