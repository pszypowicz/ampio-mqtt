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
input, but its raw-channel prefix is unconfirmed on the wire. The object still
updates through the per-object topic. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/26).

**CAN write tree device classes.** The raw write frame for binary outputs is
documented in [`protocol.md`](protocol.md) ("Panel outputs"). The CCT, DALI,
blind-calibration, panel LCD page, and alarm writes on the same `ampio/to` tree
remain unexplored. Probe notes:
[tracker](https://github.com/pszypowicz/ampio-mqtt/issues/60).

Picking one up: verify the wire shape live first (`tools/probe_config.py`
publishes candidate keywords and prints the replies), then follow the
add-an-endpoint recipe on the `ENDPOINTS` table in
[`src/ampio_mqtt/_protocol.py`](../src/ampio_mqtt/_protocol.py).
