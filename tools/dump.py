#!/usr/bin/env python3
"""Raw MQTT topic dumper for diagnosing Ampio broker access/ACLs.

Subscribes to a topic filter and prints every message received for a duration.
Optionally publishes one or more requests (e.g. the device-list request) after
subscribing. Every printed line carries the seconds elapsed since the first
request, so a slow or missing reply is visible, and an `R` marks a message the
broker replayed from its retained store rather than a live push.

Usage:
  python tools/dump.py --host ampio.lan --username U --password P --topic '#'
  python tools/dump.py --host ampio.lan --username U --password P \
      --topic 'ampio/fromDB/U/#' --request ampio/control/U/config \
      --request-payload devices --duration 15
  python tools/dump.py --topic 'device_api/from/list' \
      --request device_api/to/list --request-payload 0 --duration 15
  python tools/dump.py --topic 'ampio/from/+/state/f/+' --qos 0 --duration 5
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time

import aiomqtt


def _append_lines(path: str, lines: list[str]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Raw MQTT topic dumper for Ampio. Credentials may come from "
        "AMPIO_HOST/AMPIO_USERNAME/AMPIO_PASSWORD env vars."
    )
    p.add_argument("--host", default=os.environ.get("AMPIO_HOST"))
    p.add_argument(
        "--port", type=int, default=int(os.environ.get("AMPIO_PORT", "1883"))
    )
    p.add_argument("--username", default=os.environ.get("AMPIO_USERNAME"))
    p.add_argument("--password", default=os.environ.get("AMPIO_PASSWORD"))
    p.add_argument("--topic", default="#", help="Topic filter (default '#')")
    p.add_argument(
        "--qos",
        type=int,
        choices=(0, 1, 2),
        default=1,
        help="Subscription QoS (default 1, the library's). The broker caps a "
        "QoS 1 retained replay at its queue limit; QoS 0 receives it whole",
    )
    p.add_argument(
        "--request",
        action="append",
        default=None,
        metavar="TOPIC",
        help="Topic to publish to after subscribing; repeat for a sweep",
    )
    p.add_argument(
        "--request-payload",
        default="",
        help="Payload for every --request (default empty)",
    )
    p.add_argument("--duration", type=float, default=15.0)
    p.add_argument(
        "--max",
        type=int,
        default=0,
        help="Stop after this many messages. 0 means no limit (default)",
    )
    p.add_argument(
        "--outfile",
        default=None,
        help="Append full flag\\ttopic\\tpayload lines to this file",
    )
    args = p.parse_args()
    if not args.host:
        p.error("missing --host (or AMPIO_HOST env)")
    return args


async def run(a: argparse.Namespace) -> int:
    count = 0
    retained = 0
    try:
        async with aiomqtt.Client(
            hostname=a.host,
            port=a.port,
            username=a.username,
            password=a.password,
            identifier="ampio_mqtt_dump",
            timeout=10,
        ) as client:
            await client.subscribe(a.topic, qos=a.qos)
            print(
                f"Subscribed to {a.topic!r} at QoS {a.qos}. Listening {a.duration}s ..."
            )
            started = time.monotonic()
            for request in a.request or ():
                await client.publish(request, a.request_payload.encode(), qos=1)
                print(f"Published {a.request_payload!r} to {request!r}")

            captured: list[str] = []

            async def reader() -> None:
                nonlocal count, retained
                async for message in client.messages:
                    count += 1
                    retained += message.retain
                    flag = "R" if message.retain else " "
                    payload = message.payload.decode("utf-8", "replace")
                    topic = str(message.topic)
                    elapsed = time.monotonic() - started
                    print(f"  +{elapsed:7.3f}s {flag} {topic}  =  {payload[:200]}")
                    if a.outfile:
                        captured.append(f"{flag}\t{topic}\t{payload}\n")
                    if a.max > 0 and count >= a.max:
                        return

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(reader(), a.duration)
            if a.outfile:
                await asyncio.to_thread(_append_lines, a.outfile, captured)
    except aiomqtt.MqttError as err:
        print(f"MQTT error: {err}")
        return 1
    print(f"\nTotal messages received: {count} ({retained} retained)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
