# ampio-mqtt

> **Beta - 0.x.x.** This library exists to back the
> [home-assistant/core ampio integration](https://github.com/home-assistant/core)
> currently in development. Anything below `1.0.0` is unstable by design:
> the public surface (dataclass fields, exported names, method signatures)
> can and will change between any two 0.x.x releases without migration
> shims. `1.0.0` is reserved for the moment the integration PR is accepted
> upstream; until then, breaking changes are expected and pins should be
> exact.

Async Python client for the **Ampio Smart Home** local MQTT protocol exposed by
the Ampio M-SERV controller. Built to back a Home Assistant integration; the
library itself is Home Assistant agnostic.

> **Account tiers.** Any Ampio account works; what the library can see is
> decided by the account's administrator bit. Per-user app permissions do
> not change it (live-verified with a standard account granted every app
> permission):
>
> - **Administrator** - the full catalogue: every DB object, the module
>   list (`AmpioClient.modules` with names, models, firmware), and the
>   global raw-channel topics that deliver input events with minimal
>   latency.
> - **Standard user** (the recommended shape for a dedicated Home
>   Assistant account) - the app-sync surface: the objects the
>   administrator granted the account in the Ampio app, with names,
>   classification metadata, visibility flags, rooms, and the server
>   identity (`AmpioClient.server_info`). No module list (so `mserv_id`
>   stays `None`) and no raw input topics - input events arrive only
>   through the slower per-object republish.
>
> `AmpioClient.access_tier` reports the detected tier once discovery
> completes.
>
> **The grant bounds reads and writes alike.** An account can only read
> and only command the objects it was granted in the app; a command aimed
> at anything else is dropped, and no state for it reaches the account's
> namespace. A dedicated standard account is therefore a real privilege
> boundary, not just a narrower view.

## Status

Beta, iterated alongside the home-assistant/core ampio integration (see the
stability note above). Currently supports:

- TCP connection to the Ampio MQTT broker with username/password auth and
  auto-reconnect with capped exponential backoff and jitter,
- discovery of physical modules and logical DB objects from the M-SERV,
- two-tier discovery: administrator accounts read the full `config`
  catalogue; standard accounts fall back to the app-sync `data` surface
  (grant-filtered objects with full metadata, plus the `params_devices`
  visibility table). The detected tier is exposed as
  `AmpioClient.access_tier`,
- replacement-stable per-object identity via `AmpioObject.stable_key`
  (the Designer `leafId`), identical on both tiers - see
  [`docs/identity.md`](docs/identity.md),
- live push of object state changes via per-object MQTT topics, plus a bulk
  states snapshot at startup,
- classification of sensor objects (temperature and M-SENS environmental
  channels) with Home-Assistant-compatible device/state class hints,
- M-SERV identification (mac, firmware versions, local IP),
- best-effort LAN discovery via `discover()` (explicit multicast DNS A-record
  lookup of `ampio.local` driven by `python-zeroconf`, followed by a TCP probe
  of the resolved address). Home Assistant integrations can pass their shared
  `AsyncZeroconf` instance via `discover(zeroconf=...)`.
- per-object room mapping via `AmpioClient.fetch_rooms()` (`{object_id:
room_name}`), backed by the M-SERV's MQTT `data/groups` + `data/group_devices`
  endpoints. Intended for a Home Assistant integration to forward as
  `DeviceInfo.suggested_area` at first import; reassignment is the user's call
  after that.
- per-module capability classification on `AmpioModule.capabilities` (a
  `frozenset[Capability]`): `DIGITAL_OUTPUT`, `DIGITAL_INPUT`, `ANALOG_INPUT`,
  `TEMPERATURE_INPUT`, `ENV_SENSOR`, `ROLLER_OUTPUT`, `RGBW_OUTPUT`,
  `IR_OUTPUT`, `UI_PANEL`, `BRIDGE`, `HUB`, `ALARM`, `AUDIO_VIDEO`. Most
  modules carry several flags (e.g. M-OC-4s = `{DIGITAL_OUTPUT, ANALOG_INPUT,
RGBW_OUTPUT}`). Drives HA platform selection and bundle/split decisions in
  the integration.
- object control via `AmpioClient.command()` plus typed helpers
  (`turn_on`, `turn_off`, `toggle`, `set_value`, `set_color`,
  `open_cover`, `close_cover`, `set_cover_position`, `set_cover_tilt`). Works on both
  account tiers - see [`docs/protocol.md`](docs/protocol.md),
- output-object classification via `classify()` / `OutputKind`
  (`AmpioObject.output_kind`, `is_output`, `supports_tilt`): relays,
  dimmers, RGBW lights, and the three cover types, each flagged with the
  command verbs it answers so a consumer picks a platform without its own
  type table. Tilt-capable blinds also report their slat angle as
  `AmpioObject.tilt_position`,
- input-object classification via `classify()` / `InputKind`
  (`AmpioObject.input_kind`, `is_input`, `is_on`): flags map to a generic
  boolean, motion detection to `binary_sensor.motion`. Live flag/button events
  are delivered with minimal latency through the same `add_object_listener()`
  pipeline by routing the decoded raw per-channel topics (which fire ahead of
  the per-object republish) to the owning object.

Protocol reference notes live under [`docs/`](docs/README.md);
[`src/ampio_mqtt/const.py`](src/ampio_mqtt/const.py) remains the
canonical source for the topic helpers.

## Installation

```
pip install ampio-mqtt
```

`discover()` resolves `ampio.local` over multicast DNS from inside the
process via [`python-zeroconf`](https://pypi.org/project/zeroconf/), which
is a hard runtime dependency. The lookup works identically on macOS, HAOS,
plain Linux, and Docker - no dependency on `nss-mdns`/avahi being
configured on the host.

## Usage

```python
import asyncio

from ampio_mqtt import AmpioClient, discover


async def main() -> None:
    # Find the M-SERV on the LAN via mDNS.
    candidates = await discover()
    if not candidates:
        raise SystemExit("No Ampio M-SERV found on the LAN")
    host = candidates[0].address or candidates[0].host

    client = AmpioClient(host, username="user", password="secret")
    client.add_object_listener(lambda obj: print(obj.id, obj.kind, obj.value))
    await client.start()  # connects, subscribes, requests discovery

    # Per-object room map. A Home Assistant integration would forward each
    # value as `DeviceInfo.suggested_area` at first device creation.
    rooms = await client.fetch_rooms()
    for obj_id, room in rooms.items():
        print(f"object {obj_id} -> {room}")

    await asyncio.sleep(30)
    await client.stop()


asyncio.run(main())
```

## License

MIT
