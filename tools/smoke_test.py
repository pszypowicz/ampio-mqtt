#!/usr/bin/env python3
"""Live smoke test for ampio-mqtt against a real Ampio broker.

Connects, requests device discovery, and prints the discovered objects and the
sensor states from the states snapshot and live pushes for a fixed duration.

Usage:
  python tools/smoke_test.py --host 192.0.2.10 --username USER --password PASS
  python tools/smoke_test.py --host ampio.lan --port 1883 --duration 20

Read-only: it never publishes commands, only the discovery request.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Callable

import aiomqtt
from _session import make_client

from ampio_mqtt import (
    AmpioAdminClient,
    AmpioConnectionError,
    AmpioNotConfigured,
    AmpioObject,
    ObjectUpdated,
    SensorKind,
    format_mac,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live smoke test for ampio-mqtt against a real Ampio broker. "
        "Credentials may come from AMPIO_HOST/AMPIO_PORT/AMPIO_USERNAME/"
        "AMPIO_PASSWORD env vars."
    )
    p.add_argument("--host", default=os.environ.get("AMPIO_HOST"), help="Broker host")
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AMPIO_PORT", "1883")),
        help="Broker port (default: AMPIO_PORT, else 1883)",
    )
    p.add_argument(
        "--username",
        default=os.environ.get("AMPIO_USERNAME"),
        help="Account name (default: AMPIO_USERNAME)",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("AMPIO_PASSWORD"),
        help="Account password (default: AMPIO_PASSWORD)",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=20.0,
        help="Seconds to listen for state after connecting (default 20)",
    )
    p.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = p.parse_args()
    if not args.host:
        p.error("missing --host (or AMPIO_HOST env)")
    if not args.username:
        # Report a missing username as an argument error before the client
        # constructor refuses it.
        p.error("missing --username (or AMPIO_USERNAME env)")
    return args


async def run(
    args: argparse.Namespace,
    client_factory: Callable[[], aiomqtt.Client] | None = None,
) -> int:
    """Drive the run; ``client_factory`` is the test seam for the session."""
    client = make_client(
        args.host,
        args.username,
        args.password,
        port=args.port,
        client_factory=client_factory,
    )

    def on_object(obj: AmpioObject) -> None:
        if isinstance(obj.kind, SensorKind) and obj.state is not None:
            print(f"  state  ob/{obj.id:<5} {obj.kind.key:<14} = {obj.state}")

    client.subscribe(lambda e: on_object(e.object), of=ObjectUpdated)

    print(f"Connecting to {args.host}:{args.port} ...")
    try:
        await client.connect(timeout=15)
        print("Connected. Listening for discovery + state...\n")

        await asyncio.sleep(args.duration)

        objs = client.objects
        types: dict[str, int] = {}
        for o in objs.values():
            types[o.typ_komponentu] = types.get(o.typ_komponentu, 0) + 1
        sensors = [o for o in objs.values() if isinstance(o.kind, SensorKind)]
        print(f"\n=== Client: {type(client).__name__} ===")
        # The module catalogue answers the admin login alone, so only the
        # admin client holds one.
        modules = len(client.modules) if isinstance(client, AmpioAdminClient) else 0
        print(
            f"=== Objects: {len(objs)} (sensors: {len(sensors)}), "
            f"modules: {modules} ==="
        )
        print("  by typ_komponentu:", types)

        print("\n=== Sensors (auto-discovered) ===")
        for o in sorted(sensors, key=lambda o: o.id):
            kind = o.kind
            if not isinstance(kind, SensorKind):
                continue
            unit = kind.unit or ""
            dc = kind.device_class or "-"
            print(f"  ob/{o.id:<5} {dc:<18} {o.name!s:<26} = {o.state} {unit}")
        return 0
    except AmpioConnectionError as err:
        print(f"FAILED to connect: {err}")
        return 1
    except AmpioNotConfigured as err:
        for oid, name in err.objects:
            print(f"not configured: ob/{oid} {name or ''}")
        for mac, ids in err.collisions:
            print(
                f"mac collision: {format_mac(mac)} on modules "
                f"{', '.join(map(str, ids))}"
            )
        return 1
    finally:
        await client.disconnect()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
