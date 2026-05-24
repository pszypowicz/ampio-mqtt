# Ampio MQTT protocol (as used by node-red-contrib-ampio 0.6.8)

Source of truth: the open-source `node-red-contrib-ampio` (github.com/ampio-com/node-red-contrib-ampio, ISC, author Ampio Sp. z o.o.). This is Ampio's published integration protocol, used by the M-SERV / Ampio server MQTT bridge. Captured for a clean reimplementation in `aioampio`.

## Connection

- Broker: `mqtt://<host>:1883` (TCP, default port 1883) or a local unix socket `/var/run/ampio/mqtt.sock`. For an external client use TCP.
- Auth: username + password (Ampio account / Smart Home Manager credentials), validated by the broker's Ampio auth plugin.
- Client options: random clientId (`AmpioNode_<hex>`), clean session, `reconnectPeriod` ~5 s, QoS 0.

## Identifiers

- `mac`: device CAN id, hex string, UPPERCASE, leading zeros stripped (e.g. `1A2B`). In code: `sanitize_mac`.
- `ioid`: 1-based input/output index, leading zeros stripped. In code: `sanitize_ioid`.
- `valtype`: value-type key (see below).

## Discovery

1. Device list: subscribe `ampio/from/can/dev/list`, publish empty `ampio/to/can/dev/list`. Response payload is JSON:
   `{"devices": [ {"user_mac", "typ", "name"(base64 UTF-8), "soft_ver", "protocol", "bi","bo","ai","ao","f"}, ... ]}`
   - `typ` -> device type (see `db/devtypes.json`): gives `type` (model name), `inoptions` (readable valtypes), `outoptions` (command valtypes), optional `rt` (roller count).
   - `protocol` >= 22 enables `afu8` (analog flags).
   - `bi/bo/ai/ao/f` = counts of binary in / binary out / analog in / analog out / flags.
2. Per-device descriptions (friendly names of each I/O): subscribe `ampio/from/<mac>/description`, publish empty `ampio/to/<mac>/description`. Response is a JSON object keyed `"<typecode>_<index>"` -> base64 name. Typecodes per valtype (the `con` arrays): outputs/state `[12,13,16,17]`, temperature `[3]`, digital input `[1,2,10,11]`, analog `[14,15,16,17]`, flag `[6]`, rollers `[16,17]`, IR `[21]`. Indices for typecodes in `[11,13,15,17]` are offset by +255 (second bank). Unnamed entries are absent/blank.

## State (read) - immediate push, only on change

Topic: `ampio/from/<mac>/state/<valtype>/<ioid>` (or `ampio/from/<mac>/raw`).
Payload: the value as a UTF-8 string. (Exact numeric formatting per type to be confirmed against live capture; environmental values are human units per the labels below.)

valtypes (`db/invaltypes.json`):

- `o` Digital output, `i` Digital input, `re` Digital input rising-edge (alias of `i` + edge detect)
- `t` Temperature [C] (also `temp` -> `t`/ioid 1)
- `a` Analog value; `au` 8-bit, `au16` 16-bit, `au32` 32-bit analog; `afu8` analog flag (protocol >= 22)
- `f` Flag; `rgbw` RGB color; `rs` Heating/Cooling zone set temperature
- M-SENS environmental (device type 44), each maps to MQTT valtype `au16l` with a fixed ioid:
  `hum`->1 (Humidity %), `absp`->2 (abs pressure hPa), `relp`->6 (rel pressure hPa), `db`->3 (Loudness dB), `lux`->4 (Brightness lux), `iaq`->5 (IAQ), `co2`->7 (CO2 ppm), `temp`->`t`/1 (Temperature C)

## Commands (write) - for later platforms

Topic base `ampio/to/<mac>`:

- Digital output / relay (`s`): `ampio/to/<mac>/o/<ioid>/cmd`
- Flag (`f`), roller set/down/move (`rs`/`rsdn`/`rm`): `ampio/to/<mac>/<valtype>/<ioid>/cmd`
- Raw (`r`): `ampio/to/<mac>/raw`
- IR (`ir`): raw, payload `8206<hex(ioid-1)>`
- afu8: raw, payload `7AF9<hex(value)><hex(ioid-1)>`

## Device types (db/devtypes.json) - highlights for platform mapping

- Sensors (temperature `t`): most M-IN-_, M-REL-_, M-DOT-\* panels, M-ROOM-s, M-REL-C4s.
- M-SENS (44): hum/absp/relp/db/lux/iaq/co2/temp.
- M-CON-s (25): au/au16/au32 measurements. M-IN-AD8s (29): `ai`.
- Lights: M-OC-4s (12, `rgbw`+`a`), M-DIM-\* / M-LED-1 (`a`+`o`), dimmers.
- Relays/outputs: M-REL-_ (2/4/24/62/72), M-OC-32s (41), M-OUT-4s (39), M-INOC-_.
- Rollers: M-RT-s (19-23, `rs`/`rsdn`/`rm`, `rt` count).

## Notes / to confirm live (Phase 3)

- Exact payload numeric format per valtype (e.g. temperature scaling, analog raw vs scaled).
- Exact `description` JSON shape and base64 name decoding.
- `au16l` valtype string used for M-SENS state topics (vs `au16`).
- Retain behavior on state topics (node has a "retain ignore" option).
