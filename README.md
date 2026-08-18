# ampio-mqtt

Async Python client for the **Ampio Smart Home** local MQTT protocol
exposed by the Ampio M-SERV controller. Built to back a Home Assistant
integration while staying Home Assistant agnostic itself.

> **Beta.** Everything below `1.0.0` may break between any two releases
> without migration shims, so pin exact versions. `1.0.0` is reserved for
> the release that accompanies the
> [home-assistant/core](https://github.com/home-assistant/core)
> integration being accepted upstream.

## Installation

```
pip install ampio-mqtt
```

## Quickstart

```python
import asyncio

from ampio_mqtt import AmpioClient, ObjectUpdated, discover


async def main() -> None:
    candidates = await discover()  # mDNS lookup of ampio.local
    if not candidates:
        raise SystemExit("No Ampio M-SERV found on the LAN")
    host = candidates[0].address or candidates[0].host

    client = AmpioClient(host, username="user", password="secret")
    client.subscribe(
        lambda e: print(e.object.id, e.object.kind, e.object.value),
        of=ObjectUpdated,
    )
    await client.start()  # connect, subscribe, run discovery

    rooms = await client.fetch_rooms()
    for obj_id, room in rooms.items():
        print(f"object {obj_id} -> {room}")

    await asyncio.sleep(30)
    await client.stop()


asyncio.run(main())
```

## What it does

Each area is one page under [`docs/`](docs/README.md), and the docstrings
carry the API detail.

- A maintained broker connection with QoS 1 on every leg, capped-backoff
  reconnect, and one typed event stream that includes the terminal
  `AuthFailed` and `ConnectionDied` signals
  ([`docs/events.md`](docs/events.md)).
- Discovery of the object and module catalogues on either account tier,
  with the detected tier exposed for setup flows
  ([`docs/account-tiers.md`](docs/account-tiers.md)).
- Classification of every object into a sensor, input, output, or
  thermostat kind with Home-Assistant-compatible hints
  ([`docs/classification.md`](docs/classification.md)).
- Replacement-stable identity for objects and modules, so a hardware swap
  keeps its entities ([`docs/identity.md`](docs/identity.md)).
- Commands for relays, dimmers, RGBW lights, covers with stop and tilt,
  the regulator setpoint, scenes, and bus events, plus a raw escape hatch
  for the rest of the verb vocabulary
  ([`docs/protocol.md`](docs/protocol.md)).
- A low-latency input bridge from the raw per-channel topics on the admin
  tier ([`docs/raw-channel-bridge.md`](docs/raw-channel-bridge.md)).
- Room mapping, per-module health, eviction events for server-side
  deletions, and connection diagnostics for a consumer's report blob.
- LAN discovery of the M-SERV by multicast DNS, self-contained in the
  process ([`docs/discovery-flow.md`](docs/discovery-flow.md)).

## Choosing an account

A dedicated standard account is the recommended shape for Home Assistant.
It sees exactly the objects granted in the Ampio app and can command only
those. An administrator account adds the module list and the low-latency
raw input topics. Bus events are the exception on both tiers, since any
account can raise any event number and the logic behind an event runs
with full authority. [`docs/account-tiers.md`](docs/account-tiers.md) has
the capability table and the measured latency difference.

## Supported M-SERV versions

The library is developed and live-tested against an M-SERV self-reporting
`serverVersion` 1865 (`serverRevision` 409, `mqttVersion` 5.133.11). That
baseline is the compatibility floor; wire behavior documented in this
repo is verified against that install unless marked otherwise in place -
an open claim carries a tracking-issue link or a caveat naming exactly
what is unverified. Older servers are not supported, and
the library logs a warning when the connected server reports a lower or
missing `serverVersion`. If something misbehaves on an older server,
upgrade the M-SERV first.

## License

MIT
