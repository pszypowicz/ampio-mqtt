#!/usr/bin/env python3
"""Probe the Ampio DB-object API for additional endpoints.

Discovery on this protocol works by publishing a keyword to
``ampio/control/<user>/<surface>`` and reading the reply on
``ampio/fromDB/<user>/<surface>/<keyword>``. The endpoint table in
``src/ampio_mqtt/endpoints.py`` holds the confirmed keywords; this tool
fuzzes candidate keywords the library does not consume yet (see
docs/untapped-surfaces.md). ``--surface data`` probes the app-sync surface,
which answers on every account tier; ``config`` is administrator-only.

All requests are read-oriented config queries (the same mechanism the official
app/Matter bridge uses for discovery); nothing here changes device state.

Usage:
  python tools/probe_config.py --host H --username U --password P
  python tools/probe_config.py --keywords devices,rooms,groups --duration 8

Credentials default to AMPIO_HOST / AMPIO_PORT / AMPIO_USERNAME / AMPIO_PASSWORD.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os

import aiomqtt

DEFAULT_KEYWORDS = [
    "devicesDetails",
    "devices",
    "devicesList",
    "device",
    "rooms",
    "room",
    "locations",
    "location",
    "lokalizacje",
    "groups",
    "group",
    "grupy",
    "urzadzenia",
    "userData",
    "user",
    "data",
    "structure",
    "menu",
    "tree",
    "config",
    "settings",
    "info",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--host", default=os.environ.get("AMPIO_HOST"))
    p.add_argument(
        "--port", type=int, default=int(os.environ.get("AMPIO_PORT", "1883"))
    )
    p.add_argument("--username", default=os.environ.get("AMPIO_USERNAME"))
    p.add_argument("--password", default=os.environ.get("AMPIO_PASSWORD"))
    p.add_argument(
        "--surface",
        choices=("config", "data"),
        default="config",
        help="Control surface to probe (config is admin-only; data answers "
        "for every tier)",
    )
    p.add_argument(
        "--keywords",
        default=None,
        help="Comma-separated keywords to try (default: a built-in candidate list)",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Seconds to listen for replies after publishing all keywords",
    )
    p.add_argument(
        "--gap",
        type=float,
        default=0.4,
        help="Seconds to wait between publishing each keyword",
    )
    p.add_argument(
        "--preview",
        type=int,
        default=300,
        help="Max characters of each reply payload to print",
    )
    args = p.parse_args()
    if not args.host:
        p.error("missing --host (or AMPIO_HOST env)")
    if not args.username:
        p.error("missing --username (or AMPIO_USERNAME env)")
    return args


async def run(a: argparse.Namespace) -> int:
    user = a.username
    keywords = (
        [k.strip() for k in a.keywords.split(",") if k.strip()]
        if a.keywords
        else DEFAULT_KEYWORDS
    )
    control_topic = f"ampio/control/{user}/{a.surface}"
    reply_filter = f"ampio/fromDB/{user}/{a.surface}/#"
    seen: set[str] = set()

    try:
        async with aiomqtt.Client(
            hostname=a.host,
            port=a.port,
            username=a.username,
            password=a.password,
            identifier="ampio_mqtt_probe",
            timeout=10,
        ) as client:
            # QoS 1 keeps the broker's at-least-once leg, matching the library.
            await client.subscribe(reply_filter, qos=1)
            print(f"Subscribed to {reply_filter!r}")

            async def reader() -> None:
                async for message in client.messages:
                    payload = message.payload.decode("utf-8", "replace")
                    topic = str(message.topic)
                    sub = topic.rsplit("/", 1)[-1]
                    seen.add(sub)
                    print(f"\n<<< {topic}  ({len(payload)} bytes)")
                    print(f"    {payload[: a.preview]}")

            async def publisher() -> None:
                for kw in keywords:
                    await client.publish(control_topic, kw.encode(), qos=1)
                    print(f">>> requested {kw!r}")
                    await asyncio.sleep(a.gap)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(reader(), publisher()),
                    a.gap * len(keywords) + a.duration,
                )
    except aiomqtt.MqttError as err:
        print(f"MQTT error: {err}")
        return 1

    print(f"\n=== replies received for keywords: {sorted(seen)} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
