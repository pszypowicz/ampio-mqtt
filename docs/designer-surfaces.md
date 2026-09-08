# Designer surfaces

This page continues [`protocol.md`](protocol.md) with the legacy bridge
endpoints and the surfaces the Designer itself uses.

## Legacy CAN bridge surfaces

Two request/response endpoints predate the `fromDB` catalogues. The Node-RED
palette (`node-red-contrib-ampio`) is their public consumer. Both are
admin-gated like the rest of the `ampio/to` tree.

**Module discovery** - it still answers on the baseline install. Publish `1` to
`ampio/to/can/dev/list`. The reply arrives, not retained, on
`ampio/from/can/dev/list`:

```json
{"devices": [{"mac": "C0DE", "user_mac": "1", "typ": 10, "pcb": 7,
              "date_prod": ..., "protocol": 23, "soft_ver": ...,
              "name": "<base64>"}]}
```

Macs are uppercase hex strings, and `user_mac` is the override (the M-SERV
reports its factory mac with override `1`). `typ` is the same enumeration as
`typ_urzadzenia` - every code on the baseline install resolves in
`_devtypes.json`. Older firmware wrapped the list as `{"s": ..., "d": [...]}`
with per-module capability counts (`i`, `o`, `a`, `au`, `t`, `f`). Both are
gone. Its value: a module enumeration independent of the `fromDB` config
surface, usable as a resolver cross-check.

**Per-module descriptions** - the palette documents it as an empty publish to
`ampio/to/<MAC>/description`, answered on `ampio/from/<MAC>/description` with a
JSON object keyed `<descType>_<index>` (base64 names). The palette reads
descTypes 12, 13, 16, 17 for outputs, 6 for flags, and 21 for IR. It indexes
descTypes 11, 13, 15, 17 from 256. On the baseline install the surface is
**dead**: no reply and nothing retained, for either mac case, an empty payload,
and a wildcard reply subscription. Read names through the `device_api` record
instead (see [`description-records.md`](description-records.md)). The palette's
contract is recorded here for older bridge firmware only.

## The Designer's own surfaces

The Designer (served by the M-SERV, bundle at `/assets/index-*.js`) is an
ordinary MQTT client of the same broker. Everything it does is observable and
reproducible.

**Transport.** Plain MQTT over websocket via mqtt.js, one connection for
everything. Locally that is `ws://<host>:9001` (alongside TCP 1883). Through
Ampio's cloud it is
`wss://<device_id>-0.<cloud-domain>:6214/?_cloud_access_token=...`, plus a
support tunnel on `cloud3.ampio.com`. Connect options: MQTT 3.1.1,
`keepalive: 180`, `clean: true`, `reconnectPeriod: 2000`, no will, QoS 0
throughout. The publish wrapper drops messages while disconnected instead of a
queue.

**Surfaces.** The Designer never publishes to the `ampio/control/<user>/api`
command surface - the `/api/set` strings in the bundle are only the embedded
OpenAPI spec. It works on:

- Config reads and saves: `ampio/control/admin/config/...`, with replies on
  `ampio/fromDB/admin/config/#`. This includes the `save/leaves` table that maps
  every output leaf to command function 48 = `0x30` - the frame documented under
  Panel outputs in [`panel-writes.md`](panel-writes.md).
- The `device_api` tree: `get_data`, `name_wr`, `descriptions_wr`,
  `firmware_wr`, `mac_user_wr`, `ow_search`, plus the broadcast helpers (`list`,
  `discover`, `version`, `alive`, `devices_log`).
- The JSON-RPC pair
  `rpc/v1/ctx/admin/{call,response}/com.ampio.mserv.rpc.mqtt.restricted` (and a
  `.system.restricted` twin). Its methods include `device_raw_api`,
  `config_get`/`config_set`/`config_reload`, `sf_get`, and `params_set`, with
  `devices_status` notifications.
- Raw CAN writes: `ampio/to/<machex>/raw` and `rawf`, hex-encoded frames. The
  live-control vocabulary: the generic output write
  `[0x30, 0xF9, value, channel]`, DALI set `[57, 0xF9, ch, val]`, and the module
  identify pair `[0x7E, 1|0]` behind the Devices tab's "Identify device" button
  (Module identify in [`panel-writes.md`](panel-writes.md)). MLED-capable panels
  add an MLED family `[54, 0xDF, 1|2|3, ...]`, and flash config transfer is
  `[dst, 0xFB|0xFC, blockLo, blockHi, ...]`. The Designer also sends raw CAN
  frames to `hw/out` (first byte the send-with-id opcode, then `0x80|len`, a
  32-bit CAN id, and the data).
- Flag writes carry their own function, `0x16`. An `/api` flag write makes the
  M-SERV emit six `hw/out` frames to the module that owns the flag. They are
  parts 0 to 5 of `[0x16, part, b, b]`. The parts reassemble to a header, a
  32-bit flag mask, and one value byte (`FF` on, `00` off). The mask bit is the
  0-based flag index, one below the 1-based raw `f` channel, the same rule
  outputs follow (see Panel outputs in [`panel-writes.md`](panel-writes.md)). A
  verbatim replay of those frames drives the flag, but only through `hw/out`,
  because `ampio/to/<machex>/raw` and `rawf` drop function `0x16` while they
  accept `0x30`. The replay is also slower than `/api`, with six publishes
  against one and a median state echo of 68 ms against 40 ms. The library
  therefore keeps `/api` for flags.
- Raw feeds: `fc` / `fcocb`, `ampio/from/+/raw`, and the same `ampio/from` state
  tree this library consumes. The `raw` leaf feeds the Designer CAN packet
  monitor. Designer decodes two of its frames: the family-9 subtype-1 frame that
  carries a module's IPv4 address, and the MLED events (family 54, second byte
  `0xDF`). The leaf itself is described in
  [`raw-channel-bridge.md`](raw-channel-bridge.md).
