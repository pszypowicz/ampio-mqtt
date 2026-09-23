# Untapped surfaces

The M-SERV exposes more surfaces than the library consumes. This page lists the
known unconsumed surfaces with enough context to read alone. The links point to
the work-tracking notes, for contributors only.

**`resources` and `icons` settings tables.** Two more app-sync tables on the
`data` request surface, served to both account tiers. The library does not fetch
them. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/22).

**Server-side event log.** The M-SERV publishes its own event log on
`logs/v1/ampio-server/info`, as JSON carrying `lvl`, `mod`, `msg` and `ts`. The
feed is not retained and it is silent until something happens, so there is no
backlog to fetch. The topic sits outside the account namespace, so an
administrator account alone receives it. The library does not subscribe. Probe
notes: [tracker](https://github.com/pszypowicz/ampio-mqtt/issues/23).

**Presence simulation and detection.** The M-SERV creates two system rows of its
own, `detekcja` and `symulacja`. Neither row is a module output, and neither
carries a leaf. The library drops both by their type as it reads the catalogue,
so discovery never checks either row for a leaf. No object represents them, and
the missing leaf raises nothing. The Ampio app configures them over three
surfaces the library does not consume. The simulation switch rides
`/api/json/simulation/active` and `/api/json/simulation/deactive` on the `api`
control topic. The device lists ride the `simulation` and `detection` topics of
the account's `control` namespace. The wire facts are in
[`visibility.md`](visibility.md). The detection engine stays unverified. Its
state is a home-status code, code 5 is "home empty", and no single sensor pulse
moved it in forty minutes. The simulation replay is unverified too. Scope notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/270).

**CAN write tree device classes.** [`panel-writes.md`](panel-writes.md)
documents the raw write frames for binary outputs, the panel buzzer and panel
colors. It also documents the touch lock, module identify and the cover roller
lock. Its sections are "Panel outputs", "Panel buzzer", "Panel colors", "Touch
lock", "Module identify" and "Cover roller lock". The DALI write and the module
parameter writes on the same `ampio/to` tree remain unexplored. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/60).

**M-SERV display lines.** The OpenAPI spec declares `/api/set/setLcdUp/<text>`
and `/api/set/setLcdDown/<text>`, for the upper and lower field of a panel
display. The lower field accepts digits and a comma alone. Neither path carries
a device id, so one call likely reaches every display. The Designer never calls
either one. The baseline install carries no display panel, so neither path is
verified. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/63).

**`ampio/from/<MAC>/raw` leaf.** The M-SERV mirrors a CAN frame whose first byte
is not the broadcast byte `0xFE` onto this leaf as ASCII hex. The frame arrives
at QoS 1 and is not retained. On the baseline install, two modules emit one. The
M-SERV sends a three-byte frame every 5 s, of the family that Designer names
`mqtt`. The M-CON-s on firmware 908 sends one every 10 s. Designer feeds the
leaf to its CAN packet monitor and decodes only a module's IPv4 report and MLED
events. The library does not subscribe. A subscription adds about 0.3 messages
per second, and it gives `last_seen` to the M-SERV row and to that one module.
Probe notes: [tracker](https://github.com/pszypowicz/ampio-mqtt/issues/188).

**The rest of the `params` blob.** Every module's stored settings ride a base64
`params` field in the `device_api` list reply. The library decodes the touch
panel section and the roller section. The other sections are the power-on
defaults for a module's outputs and flags, and the open-collector and LED
curves. The rest are the relay and output maps, the real-time clock, and the
serial port mode. The Designer's own layout table gives the offsets, and the
meaning of every one of those sections is unverified. Each needs a live proof
before it can ship:
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
