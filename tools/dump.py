#!/usr/bin/env python3
"""Raw MQTT topic dumper for diagnosing Ampio broker access/ACLs.

Subscribes to a topic filter and prints every message received for a duration.
Optionally publishes one request (e.g. the device-list request) after subscribing.

Usage:
  python tools/dump.py --host ampio.lan --username U --password P --topic '#'
  python tools/dump.py --host ampio.lan --username U --password P \
      --topic 'ampio/fromDB/U/#' --request ampio/control/U/config \
      --request-payload devices --duration 15
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os

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
        "--request",
        default=None,
        help="Optional topic to publish to after subscribing",
    )
    p.add_argument(
        "--request-payload",
        default="",
        help="Payload for --request (default empty)",
    )
    p.add_argument("--duration", type=float, default=15.0)
    p.add_argument("--max", type=int, default=200, help="Max messages to print")
    p.add_argument(
        "--outfile", default=None, help="Append full topic\\tpayload lines to this file"
    )
    args = p.parse_args()
    if not args.host:
        p.error("missing --host (or AMPIO_HOST env)")
    return args


async def run(a: argparse.Namespace) -> int:
    count = 0
    try:
        async with aiomqtt.Client(
            hostname=a.host,
            port=a.port,
            username=a.username,
            password=a.password,
            identifier="ampio_mqtt_dump",
            timeout=10,
        ) as client:
            # QoS 1 keeps the broker's at-least-once leg, matching the library.
            await client.subscribe(a.topic, qos=1)
            print(f"Subscribed to {a.topic!r}. Listening {a.duration}s ...")
            if a.request:
                await client.publish(a.request, a.request_payload.encode(), qos=1)
                print(f"Published {a.request_payload!r} to {a.request!r}")

            captured: list[str] = []

            async def reader() -> None:
                nonlocal count
                async for message in client.messages:
                    count += 1
                    payload = message.payload.decode("utf-8", "replace")
                    topic = str(message.topic)
                    print(f"  {topic}  =  {payload[:200]}")
                    if a.outfile:
                        captured.append(f"{topic}\t{payload}\n")
                    if count >= a.max:
                        return

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(reader(), a.duration)
            if a.outfile:
                await asyncio.to_thread(_append_lines, a.outfile, captured)
    except aiomqtt.MqttError as err:
        print(f"MQTT error: {err}")
        return 1
    print(f"\nTotal messages received: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
