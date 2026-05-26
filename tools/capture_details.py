#!/usr/bin/env python3
"""Capture the Ampio per-user devicesDetails object list to a file.

Subscribes to ampio/fromDB/<user>/config/devicesDetails, requests it via
ampio/control/<user>/config = "devicesDetails", and writes the JSON payload
to --out for inspection. Read-only apart from the discovery request.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import aiomqtt


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Capture Ampio devicesDetails. Credentials may come from "
        "AMPIO_HOST/AMPIO_USERNAME/AMPIO_PASSWORD env vars instead of flags."
    )
    p.add_argument("--host", default=os.environ.get("AMPIO_HOST"))
    p.add_argument("--port", type=int, default=int(os.environ.get("AMPIO_PORT", "1883")))
    p.add_argument("--username", default=os.environ.get("AMPIO_USERNAME"))
    p.add_argument("--password", default=os.environ.get("AMPIO_PASSWORD"))
    p.add_argument("--out", default="devicesDetails.json")
    p.add_argument("--timeout", type=float, default=8.0)
    args = p.parse_args()
    missing = [n for n in ("host", "username", "password") if not getattr(args, n)]
    if missing:
        p.error(f"missing credentials: {', '.join(missing)} (set flags or AMPIO_* env)")
    return args


async def run(a: argparse.Namespace) -> int:
    cfg_topic = f"ampio/fromDB/{a.username}/config/devicesDetails"
    req_topic = f"ampio/control/{a.username}/config"
    async with aiomqtt.Client(
        hostname=a.host,
        port=a.port,
        username=a.username,
        password=a.password,
        identifier="ampio_mqtt_capture",
        timeout=10,
    ) as client:
        await client.subscribe(cfg_topic)
        await client.publish(req_topic, b"devicesDetails")
        print(f"Requested {cfg_topic} ...")

        async def wait_for_details() -> bytes | None:
            async for message in client.messages:
                if str(message.topic) == cfg_topic:
                    return message.payload
            return None

        try:
            payload = await asyncio.wait_for(wait_for_details(), a.timeout)
        except TimeoutError:
            print("TIMEOUT: no devicesDetails received (ACL? wrong topic?)")
            return 1

    if not payload:
        print("No payload")
        return 1
    text = payload.decode("utf-8", "replace")
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        data = json.loads(text)
        lst = data.get("List", [])
        print(f"Status={data.get('Status')} List items={len(lst)} written to {a.out}")
        if lst:
            print("First item keys:", sorted(lst[0].keys()))
            print("First item:", json.dumps(lst[0], ensure_ascii=False)[:400])
    except ValueError:
        print(f"Wrote {len(text)} bytes (not JSON?) to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
