# aioampio

Async Python client for the **Ampio Smart Home** MQTT protocol (as published by
`node-red-contrib-ampio`). Built to back a Home Assistant integration; the
library itself is Home Assistant agnostic.

## Status

Early work in progress. Implements:

- connection to the Ampio MQTT broker (TCP, username/password) with auto-reconnect,
- device discovery (`ampio/to|from/can/dev/list`),
- sensor state tracking via the retained `ampio/from/<mac>/state/<valtype>/<ioid>`
  topics, with push callbacks,
- classification of the first-platform sensor value types (temperature, M-SENS
  environmental channels, M-CON analog measurements).

See [PROTOCOL.md](PROTOCOL.md) for the captured protocol details.

## Usage

```python
import asyncio
from aioampio import AmpioClient

async def main() -> None:
    client = AmpioClient("192.0.2.10", username="user", password="secret")
    client.add_sensor_listener(lambda s: print(s.unique_id, s.value))
    await client.start()        # connects, subscribes, requests discovery
    await asyncio.sleep(30)
    await client.stop()

asyncio.run(main())
```

## License

MIT
