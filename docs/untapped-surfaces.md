# Untapped surfaces

The M-SERV exposes more surfaces than the library consumes. This page lists the
known unconsumed surfaces with enough context to read alone. The links point to
the work-tracking notes, for contributors only.

**`resources` and `icons` settings tables.** Two more app-sync tables on the
`data` request surface, served to both account tiers. The library does not fetch
them. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/22).

**Server-side event log.** The M-SERV keeps an event log behind the REST
`logbook` endpoint of its web app. A read of that log over MQTT (`fetch_logs()`)
is unexplored. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/23).

**`symulacja` raw prefix.** The presence-simulation object classifies as an
input, but its raw-channel prefix is unverified on the wire. The object still
updates through the per-object topic. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/26).

**CAN write tree device classes.** The raw write frames for binary outputs, the
panel buzzer, and module identify are documented in
[`panel-writes.md`](panel-writes.md) ("Panel outputs", "Panel buzzer", "Module
identify"). The CCT, DALI, blind-calibration, panel LCD page, and alarm writes
on the same `ampio/to` tree remain unexplored. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/60).

**`ampio/from/<MAC>/raw` leaf.** The M-SERV mirrors a CAN frame whose first byte
is not the broadcast byte `0xFE` onto this leaf as ASCII hex, at QoS 1 and not
retained. On the baseline install, two modules emit one. The M-SERV sends a
three-byte frame every 5 s, of the family that Designer names `mqtt`. The
M-CON-s on firmware 908 sends one every 10 s. Designer feeds the leaf to its CAN
packet monitor and decodes only a module's IPv4 report and MLED events. The
library does not subscribe. A subscription would add about 0.3 messages per
second and would give `last_seen` to the M-SERV row and to that one module.
Probe notes: [tracker](https://github.com/pszypowicz/ampio-mqtt/issues/188).

**The rest of the `params` blob.** Every module's stored settings ride a base64
`params` field in the `device_api` list reply. The library decodes the touch
panel section and the roller section. The other sections are the power-on
defaults for a module's outputs and flags, the open-collector and LED curves,
the relay and output maps, the real-time clock, and the serial port mode. The
Designer's own layout table gives the offsets, and the meaning of every one of
those sections is unverified. Each needs a live proof before it can ship:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/195).

Picking one up takes three steps.

1. Find the topic. The M-SERV serves the Designer at its own root. That bundle
   contains the literal topic string for every surface the vendor's own app
   uses. Read the topics out of the bundle instead of guessing keywords.
2. Verify the wire shape live. `tools/dump.py` subscribes to a filter, publishes
   one request, and prints the replies:

   ```sh
   uv run python tools/dump.py --topic 'ampio/fromDB/admin/config/#' \
       --request ampio/control/admin/config --request-payload devices
   ```

3. Follow the add-an-endpoint recipe on the `ENDPOINTS` table in
   [`src/ampio_mqtt/_protocol.py`](../src/ampio_mqtt/_protocol.py).
