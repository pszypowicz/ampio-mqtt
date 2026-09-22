# ampio-mqtt

Async Python client for the **Ampio Smart Home** local MQTT protocol exposed by
the Ampio M-SERV controller. Built to back a Home Assistant integration while
staying Home Assistant agnostic itself.

> **Beta.** Everything below `1.0.0` can break between any two releases without
> migration shims, so pin exact versions. `1.0.0` is reserved for the release
> that accompanies the
> [home-assistant/core](https://github.com/home-assistant/core) integration
> being accepted upstream.

## Installation

```
pip install ampio-mqtt
```

LAN discovery (`discover()`) needs the `discovery` extra
(`pip install ampio-mqtt[discovery]`), which pulls in `zeroconf`. Home Assistant
ships `zeroconf` itself, so the integration needs no extra.

## Quickstart

```python
import asyncio

from ampio_mqtt import AmpioClient, ObjectUpdated, discover


async def main() -> None:
    found = await discover()  # mDNS lookup of ampio.local
    if found is None:
        raise SystemExit("No Ampio M-SERV found on the LAN")

    client = AmpioClient(found.address, "user", "secret")
    client.subscribe(
        lambda e: print(e.object.id, e.object.kind, e.object.state),
        of=ObjectUpdated,
    )
    await client.connect()  # connect, subscribe, request the catalogues

    rooms = await client.fetch_rooms()
    for obj_id, room in rooms.items():
        print(f"object {obj_id} -> {room}")

    await asyncio.sleep(30)
    await client.disconnect()


asyncio.run(main())
```

## What it does

Each area is one page under [`docs/`](docs/README.md), and the docstrings carry
the API detail.

- A maintained broker connection with QoS 1 on every leg but the retained raw
  state tree, and a capped-backoff reconnect. One typed event stream carries
  every update and the terminal `AuthFailed` and `ConnectionDied` signals
  ([`docs/events.md`](docs/events.md)).
- Discovery of the object catalogue on either account tier (the module catalogue
  is admin-only), with `AmpioAdminClient` for the reserved login
  ([`docs/account-tiers.md`](docs/account-tiers.md)).
- Classification of every object into a sensor, input, output, or thermostat
  kind with Home-Assistant-compatible hints
  ([`docs/classification.md`](docs/classification.md)).
- Replacement-stable identity for objects and modules, so a hardware swap keeps
  its entities ([`docs/identity.md`](docs/identity.md)).
- Commands for relays, dimmers, RGBW and color-temperature lights, covers with
  stop and tilt with the roller lock, the regulator setpoint, scenes, and bus
  events. A push notification to the install's mobile app rides the same
  surface. The M-DOT panel buzzer, its touch field colours and touch lock, and
  the module identify LED are admin-only. The `command()` escape hatch sends any
  other `/api` verb ([`docs/commands.md`](docs/commands.md)).
- A low-latency input bridge from the raw per-channel topics on
  `AmpioAdminClient`
  ([`docs/raw-channel-bridge.md`](docs/raw-channel-bridge.md)).
- Room mapping, per-module health, reported capabilities, touch panel settings
  and cover travel parameters, eviction events for server-side deletions, and
  connection diagnostics for a consumer's report blob.
- LAN discovery of the M-SERV by multicast DNS, self-contained in the process
  ([`docs/discovery-flow.md`](docs/discovery-flow.md)).

## Choosing an account

A dedicated standard account is the recommended shape for Home Assistant. It
sees exactly the objects granted in the Ampio app and can command only those.
`AmpioAdminClient` adds the module catalogue, the low-latency raw tree, the
module diagnostics, and the CAN write surfaces (panel LEDs and colours, the
buzzer, the touch lock, and the identify LED). Bus events are the exception on
both tiers. Any account can raise any event number, and the logic behind an
event runs with full authority. [`docs/account-tiers.md`](docs/account-tiers.md)
has the capability table and the measured latency difference.

## Testing a consumer against the library

A fixture that builds model instances by hand can hold a row the M-SERV cannot
produce. `ampio_mqtt.testing` drives a decoded reply through the store the
client itself builds, so a fixture carries what the admission door admits and
nothing else.

```python
from ampio_mqtt import AmpioAdminClient
from ampio_mqtt.testing import apply_reply, build_store

store = build_store(AmpioAdminClient)
apply_reply(store, "params_devices", params_payload)
events = apply_reply(store, "data_devices", catalogue_payload)
assert store.objects.keys() == {193}
```

`build_store` takes the client class, because the class is the account tier.
`apply_reply` names the reply the way the endpoint table does and returns the
events the reply produced. Use `parse_module_address` to derive the
`ModuleAddress` of a `leafId` token rather than to build one by hand.

## Supported M-SERV versions

The library is developed and live-tested against an M-SERV self-reporting
`serverVersion` 1865 (`serverRevision` 409, `mqttVersion` 5.133.11). That
baseline is the compatibility floor. Wire behavior documented in this repo is
verified against that install unless marked otherwise in place - an open claim
says exactly what is unverified. Older servers are not supported, and the
library logs a warning when the connected server reports a lower or missing
`serverVersion`.

Ampio does not guarantee the stability of these wire surfaces. A server update
or a module firmware update can change or remove behavior this library depends
on, without notice. Breaking changes by Ampio are a known pattern. The author of
an earlier Ampio integration
[stopped maintenance for exactly this reason](https://github.com/kstaniek/ampio-hacc/issues/2).

### Upgrade rules

- If something misbehaves on an older server, upgrade the M-SERV first.
- If your install meets the baseline and works, stay on your current versions.
  Do not chase the latest ones.
- If you decide to update anyway, make a full backup first - ideally a full
  image of the M-SERV's microSD card.

## Disclaimer

This library is an independent, best-effort project and has no affiliation with
Ampio. Use it at your own risk. It commands real hardware, and a wrong command
moves real devices.

The M-SERV itself guarantees the safety of a standard account. The broker limits
such an account to the objects granted in the Ampio app, and it denies the raw
CAN surfaces on the wire. A defect in this library cannot widen that boundary.
Bus events are the one exception (see
[Choosing an account](#choosing-an-account)).

## Development tests

Install the development dependencies with `uv sync --group dev`. Run the unit
tests with `uv run pytest`.

The MQTT integration tests start temporary Mosquitto processes on loopback
addresses. They use synthetic replies and credentials and need no Ampio
hardware. Install Mosquitto with `brew install mosquitto` on macOS or
`sudo apt-get install mosquitto` on Debian or Ubuntu. Make sure that `mosquitto`
and `mosquitto_passwd` are on `PATH`, then run `uv run pytest --mqtt -m mqtt`.

Run `uv run pytest --mqtt` to include both suites. Without `--mqtt`, pytest
skips the broker tests. With `--mqtt`, missing broker executables fail the
suite. CI runs both suites on each supported Python version.

## License

MIT
