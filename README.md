# ampio-mqtt

Async Python client for the **Ampio Smart Home** local MQTT protocol exposed by
the Ampio M-SERV controller. Built to back a Home Assistant integration; the
library itself is Home Assistant agnostic.

## Status

Stable. The 1.0.0 release freezes the public API surface (everything exported
from `ampio_mqtt.__init__`); further breaking changes ship as major versions.

Currently supports:

- TCP connection to the Ampio MQTT broker with username/password auth and
  auto-reconnect with capped exponential backoff and jitter,
- discovery of physical modules and logical DB objects from the M-SERV,
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

The MQTT topic layout is documented at the top of
[`src/ampio_mqtt/const.py`](src/ampio_mqtt/const.py).

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
