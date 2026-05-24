#!/usr/bin/env python3
"""Live smoke test for aioampio against a real Ampio broker.

Connects, requests device discovery, and prints discovered devices and any
sensor states received (from retained state topics) for a fixed duration.

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

from aioampio import AmpioClient, AmpioConnectionError, AmpioObject


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live smoke test for aioampio against a real Ampio broker. "
        "Credentials may come from AMPIO_HOST/AMPIO_USERNAME/AMPIO_PASSWORD env vars."
    )
    p.add_argument("--host", default=os.environ.get("AMPIO_HOST"), help="Broker host")
    p.add_argument(
        "--port", type=int, default=int(os.environ.get("AMPIO_PORT", "1883"))
    )
    p.add_argument("--username", default=os.environ.get("AMPIO_USERNAME"))
    p.add_argument("--password", default=os.environ.get("AMPIO_PASSWORD"))
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
    return args


async def run(args: argparse.Namespace) -> int:
    client = AmpioClient(
        args.host, args.port, args.username, args.password
    )

    def on_object(obj: AmpioObject) -> None:
        if obj.is_sensor and obj.value is not None and obj.kind is not None:
            print(f"  state  ob/{obj.id:<5} {obj.kind.key:<14} = {obj.value}")

    client.add_object_listener(on_object)

    print(f"Connecting to {args.host}:{args.port} ...")
    try:
        await client.start(timeout=15)
    except AmpioConnectionError as err:
        print(f"FAILED to connect: {err}")
        return 1
    print("Connected. Listening for discovery + retained state...\n")

    await asyncio.sleep(args.duration)

    objs = client.objects
    types: dict = {}
    for o in objs.values():
        types[o.typ_komponentu] = types.get(o.typ_komponentu, 0) + 1
    print(f"\n=== Objects: {len(objs)} (sensors: {len(client.sensors)}) ===")
    print("  by typ_komponentu:", types)

    print("\n=== Sensors (auto-discovered) ===")
    for o in sorted(client.sensors.values(), key=lambda o: o.id):
        unit = (o.kind.unit or "") if o.kind else ""
        dc = (o.kind.device_class or "-") if o.kind else "-"
        print(f"  ob/{o.id:<5} {dc:<18} {str(o.name):<26} = {o.value} {unit}")

    await client.stop()
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
