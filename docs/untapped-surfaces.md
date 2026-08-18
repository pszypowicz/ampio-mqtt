# Untapped surfaces

The M-SERV exposes more than the library consumes. The tracking issues
own the detail - wire shapes, probe results, and design notes live
there, so this page cannot drift from them.

| Surface                                                           | Issue                                                     |
| ----------------------------------------------------------------- | --------------------------------------------------------- |
| `resources` / `icons` settings tables (both tiers)                | [#22](https://github.com/pszypowicz/ampio-mqtt/issues/22) |
| Server-side event log (the REST `logbook` bridge lead)            | [#23](https://github.com/pszypowicz/ampio-mqtt/issues/23) |
| MD5 change-detection topics - skip redundant refetches            | [#24](https://github.com/pszypowicz/ampio-mqtt/issues/24) |
| `device_raw_api` RPC bridge - per-output `outLoc` area resolution | [#25](https://github.com/pszypowicz/ampio-mqtt/issues/25) |
| `symulacja` raw-channel prefix - confirm and bridge               | [#26](https://github.com/pszypowicz/ampio-mqtt/issues/26) |
| CAN write tree `ampio/to/...` - admin-only device classes         | [#60](https://github.com/pszypowicz/ampio-mqtt/issues/60) |

Picking one up: verify the wire shape live first
(`tools/probe_config.py` publishes candidate keywords and prints the
replies), then follow the add-an-endpoint recipe on the `ENDPOINTS`
table in [`src/ampio_mqtt/endpoints.py`](../src/ampio_mqtt/endpoints.py).
