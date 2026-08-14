#!/usr/bin/env python3
"""Send a command to one Ampio object and watch the resulting state.

Drives the library's own command API against a live M-SERV, printing every
state update the object emits until the watch window closes - so a cover shows
its travel, not just the final position.

Usage:
  python tools/set_object.py --object-id 64 --on
  python tools/set_object.py --object-id 48 --position 55 --watch 45
  python tools/set_object.py --object-id 50 --color 10,20,30,40
  python tools/set_object.py --object-id 135 --verb setValue --arg 255

Credentials default to AMPIO_HOST / AMPIO_PORT / AMPIO_USERNAME / AMPIO_PASSWORD.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from ampio_mqtt import AmpioClient, AmpioObject


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", default=os.environ.get("AMPIO_HOST"))
    p.add_argument("--port", type=int, default=int(os.environ.get("AMPIO_PORT", "1883")))
    p.add_argument("--username", default=os.environ.get("AMPIO_USERNAME"))
    p.add_argument("--password", default=os.environ.get("AMPIO_PASSWORD"))
    p.add_argument("--object-id", type=int, required=True, help="DB object id to command")

    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--on", action="store_true", help="turnOn")
    action.add_argument("--off", action="store_true", help="turnOff")
    action.add_argument("--toggle", action="store_true", help="switch")
    action.add_argument("--open", action="store_true", help="open a cover")
    action.add_argument("--close", action="store_true", help="close a cover")
    action.add_argument("--value", type=int, help="setValue level, 0-255")
    action.add_argument("--position", type=int, help="cover position percent, 0-100")
    action.add_argument("--color", help="RGBW as R,G,B,W (each 0-255)")
    action.add_argument("--verb", help="raw verb for anything not wrapped above")

    p.add_argument("--arg", action="append", default=[], help="argument for --verb (repeatable)")
    p.add_argument("--lamella", type=int, help="slat angle for --position; left alone if omitted")
    p.add_argument("--pulse-ms", type=int, help="revert --value after this many ms")
    p.add_argument("--watch", type=float, default=10.0, help="seconds to watch state (default 10)")
    args = p.parse_args()
    if not args.host:
        p.error("missing --host (or AMPIO_HOST env)")
    return args


async def send(client: AmpioClient, a: argparse.Namespace) -> None:
    oid = a.object_id
    if a.on:
        await client.turn_on(oid)
    elif a.off:
        await client.turn_off(oid)
    elif a.toggle:
        await client.toggle(oid)
    elif a.open:
        await client.open_cover(oid)
    elif a.close:
        await client.close_cover(oid)
    elif a.value is not None:
        await client.set_value(oid, a.value, pulse_ms=a.pulse_ms)
    elif a.position is not None:
        await client.set_cover_position(oid, a.position, lamella=a.lamella)
    elif a.color is not None:
        channels = [int(part) for part in a.color.split(",")]
        if len(channels) != 4:
            raise SystemExit("--color needs four comma-separated values: R,G,B,W")
        await client.set_color(oid, *channels)
    else:
        await client.command(oid, a.verb, *a.arg)


async def run(a: argparse.Namespace) -> int:
    client = AmpioClient(a.host, a.port, a.username, a.password)

    def on_object(obj: AmpioObject) -> None:
        if obj.id == a.object_id:
            print(f"  state  ob/{obj.id} = {obj.value}")

    client.add_object_listener(on_object)
    await client.start()
    print(f"Connected as {a.username!r} (tier: {client.access_tier.value})")

    obj = client.objects.get(a.object_id)
    print(f"before: ob/{a.object_id} = {obj.value if obj else '<not in this account view>'}")

    await send(client, a)
    print(f"command sent; watching {a.watch}s ...")
    await asyncio.sleep(a.watch)

    obj = client.objects.get(a.object_id)
    print(f"after:  ob/{a.object_id} = {obj.value if obj else '<not in this account view>'}")
    await client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
