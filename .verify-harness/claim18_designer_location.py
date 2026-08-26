"""Contract (#25): resolve_locations() populates AmpioObject.location.

Expected:
  admin: resolve -> {64: 'Potter'}; objects[64].location='Potter';
         objects[64].matter_device_type=256 (refined from the CAN record)
  restricted: resolve_locations() raises RuntimeError
"""

from __future__ import annotations

import asyncio

from ampio_mqtt import AmpioClient

from fixtures import ADMIN, PORT, USER, fake_mserv


async def main() -> None:
    ready = asyncio.Event()
    server = asyncio.create_task(fake_mserv(user=ADMIN, ready=ready))
    await asyncio.wait_for(ready.wait(), 5)
    client = AmpioClient("127.0.0.1", ADMIN, port=PORT)
    assert await client.start(timeout=5.0, discovery_timeout=5.0)
    try:
        resolved = await client.resolve_locations(timeout=5.0)
        print(f"admin: resolved={resolved}")
        print(f"admin: location={client.objects[64].location}")
        print(f"admin: matter_device_type={client.objects[64].matter_device_type}")
    finally:
        await client.stop()
        server.cancel()

    ready = asyncio.Event()
    server = asyncio.create_task(fake_mserv(user=USER, ready=ready))
    await asyncio.wait_for(ready.wait(), 5)
    restricted = AmpioClient("127.0.0.1", USER, port=PORT)
    assert await restricted.start(timeout=5.0, discovery_timeout=5.0)
    try:
        try:
            await restricted.resolve_locations(timeout=1.0)
            print("restricted: NO ERROR - CLAIM FAILED")
        except RuntimeError as err:
            print(f"restricted: RuntimeError={err}")
    finally:
        await restricted.stop()
        server.cancel()


if __name__ == "__main__":
    asyncio.run(main())
