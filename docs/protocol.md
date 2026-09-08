# Protocol surface

The M-SERV speaks two parallel topic trees on the same MQTT broker:

- **DB tree** - `ampio/fromDB/<user>/...` and `ampio/control/<user>/...`.
  User-scoped. It carries the discovery RPC pattern: publish a keyword on one of
  the control surfaces, and the matching `fromDB` topic gets a JSON response.
  Per-object live state arrives on `.../ob/<id>/state`.
- **Raw tree** - `ampio/from/#`. Global, not user-scoped, keyed by the module's
  effective bus MAC, and served to administrator accounts only. Its branches are
  the retained decoded-CAN per-channel state under `state/<prefix>/<ch>`, the
  diagnostics broadcasts under `b/<type>`, and the bus events under `event`. The
  retained state branch is the library's low-latency bridge - see
  [`raw-channel-bridge.md`](raw-channel-bridge.md).

The rest of this area is on its own pages.

| Page                                           | Subject                                                                                                                     |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| [`commands.md`](commands.md)                   | The `/api` verb vocabulary, the client method behind each verb, the state-echo confirmation, and the cover and scene notes. |
| [`panel-writes.md`](panel-writes.md)           | The raw CAN output frame for panel status LEDs, relays, and open-collector outputs, and the panel buzzer.                   |
| [`designer-surfaces.md`](designer-surfaces.md) | The legacy CAN bridge endpoints and every surface the Designer itself uses, the flag write frames included.                 |
| [`bus-events.md`](bus-events.md)               | Bus events: how to raise one, how to receive one, and which tier gets which.                                                |

All topic helpers live in
[`src/ampio_mqtt/_protocol.py`](../src/ampio_mqtt/_protocol.py). Treat the
constants there as the authoritative source. The table below is a quick
reference.

## Discovery (request / response)

Publish the keyword as the payload on the control surface. The broker publishes
the response on the matching `fromDB` topic. Most responses are retained, so a
fresh subscriber sees the last value immediately.

The whole `config` surface answers only for **administrator** accounts. Standard
accounts get silence there (no error, no reply, independent of the account's app
permissions). Everything on the `data`, `states`, and `info` surfaces answers
for every account.

| Keyword          | Control surface               | Response topic                              | Shape                                                                                                                                                                                                                                                                                                                                                             |
| ---------------- | ----------------------------- | ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `devicesDetails` | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/devicesDetails` | `{Status, List: [{id, id_urzadzenia, typ_komponentu, interpretacja, funkcja, leafId, opis_menu, type, stan_json, ...}]}` - `type` is the Matter device type tag (see [`description-records.md`](description-records.md)).                                                                                                                                         |
| `devices`        | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/devices`        | `{List: [{id, mac, mac_global, typ_urzadzenia, nazwa_urzadzenia, wersja_softu, wersja_pcb, ...}]}`                                                                                                                                                                                                                                                                |
| `locations`      | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/locations`      | `{List: [{id, opis_menu, opis_rozwiniety}]}` - Designer's "Lokalizacja" name table. The per-output pointer that resolves through it rides the `device_api` tree below (see [`description-records.md`](description-records.md)).                                                                                                                                   |
| `devices`        | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/devices`          | `{List: [...]}` - app-sync object catalogue: the `devicesDetails` row shape minus `params`, `stan_json`, and `url`, filtered to the account's app grants.                                                                                                                                                                                                         |
| `params_devices` | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/params_devices`   | `{List: [{id, params, param1, czas, powiazane, url}]}` - per-object `params` bitfields for the **full** catalogue (not grant-filtered).                                                                                                                                                                                                                           |
| `groups`         | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/groups`           | `{List: [{id, id_rodzica, opis_menu}]}` - room tree.                                                                                                                                                                                                                                                                                                              |
| `group_devices`  | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/group_devices`    | `{List: [{id_grupy, id_obiektu}]}` - object-to-room join.                                                                                                                                                                                                                                                                                                         |
| `scenes`         | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/scenes`           | `{List: [{id, parentId, sceneName, active, Actions, Infos, Schedules}]}` - scene catalogue. `Actions` are wire command strings, `Infos` their structured form.                                                                                                                                                                                                    |
| (empty)          | `ampio/control/<user>/states` | `ampio/fromDB/<user>/data/states`           | `{List: [{id, stan_json}]}` - bulk snapshot of the account's object states.                                                                                                                                                                                                                                                                                       |
| (empty)          | `ampio/control/<user>/info`   | `ampio/fromDB/<user>/data/info`             | `{Results: {mac, userId, serverVersion, serverRevision, mqttVersion, local_ip, device_id, ...}}` - server self-report, retained in the account namespace. `userId` is the asking account's id (`-1` for the reserved `admin` login). `AmpioServerInfo.access_tier` exposes it for config flows. A running client's tier is decided by its authenticated username. |

## Module description records (`device_api`)

A third topic pair sits next to the `config`/`data` request-response surfaces
and the raw tree. `device_api/to/list` with the payload `0` asks the M-SERV for
every module's description record at once. The reply lands on
`device_api/from/list` as `{devices: [...]}`. Each module entry carries
`macUser` (the override), `macProd` (the factory id), `protocol`, `name`
(base64), and `descriptions`. The last is base64 of the per-output entries
behind both the Matter device type tag and the Designer "Lokalizacja" location
pointer. The frame layout, the descType enum, and the join rule that resolves an
object to its entry are in [`description-records.md`](description-records.md).
The tree is admin-only, exactly like the raw tree.
`AmpioClient.resolve_records()` drives this pair. A consumer calls that method
and never publishes on the pair itself.

The per-module pair serves the same record for one module.
`device_api/to/<machex>/get_data` (empty payload) answers on
`device_api/from/<MACHEX>/info`. Both macs are the factory id, never the
override, lowercase hex on the request and uppercase on the reply. A module with
a Designer override stays silent on its override mac, the M-SERV's own row
included. The M-SERV serves those requests one module at a time, at a mean gap
of 0.75 seconds on the baseline install. The list reply carries the same blobs
in one message, so the library reads the list.

Each account namespace also carries a retained
`ampio/fromDB/<user>/md5/<keyword>` topic per app-sync table (`devices`,
`params_devices`, `groups`, `group_devices`, `scenes`, `resources`, `icons`,
`logging`). Each holds the MD5 of the exact reply payload the account receives,
per-account for the grant-filtered tables. The Designer uses these to skip
redundant refetches. The hashes cover neither the `config` catalogues nor
`states`, so the library saves no request with them. It reads `md5/devices` and
`md5/params_devices` as change signals instead. The Designer-save push and the
re-request rule are in [`discovery-flow.md`](discovery-flow.md).

## Live state

| Topic                                  | Payload                           | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------- | --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ampio/fromDB/<user>/ob/<id>/state`    | `{state, desc, on}`               | One per object. `state` is the value (string), `desc` is the M-SERV's pretty form, `on` is server-side ms epoch. Cover (`roleta*`) pushes carry a `block` field in place of `desc`. Regulator (`reg`) objects push a richer shape instead: `{state, cooling, mode, measureTemp, setTemperature, on}`, every field a string, exposed as `AmpioObject.thermostat`. The library exposes `state`, `lammel`, and the reg readback from these. |
| `ampio/from/<MAC>/state/f/<ch>`        | plain text (`"0"`/`"1"`)          | Flag channel, bridged to the owning object.                                                                                                                                                                                                                                                                                                                                                                                              |
| `ampio/from/<MAC>/state/i/<ch>`        | plain text (`"0"`/`"1"`)          | Digital input channel, bridged to the owning object.                                                                                                                                                                                                                                                                                                                                                                                     |
| `ampio/from/<MAC>/state/o/<ch>`        | plain text (`"0"`/`"1"`)          | Binary output channel, bridged to the owning `przekaznik` object on a binary-output leaf, class 257 (a panel status LED, or a relay output).                                                                                                                                                                                                                                                                                             |
| `ampio/from/<MAC>/state/a/<ch>`        | plain text (u8, `"0"` to `"255"`) | Analog output channel, subscribed on the admin tier and bridged to the owning `przekaznik` object on an open-collector leaf (class 67). Every other `a` channel drops at the index lookup.                                                                                                                                                                                                                                               |
| `ampio/from/<MAC>/state/{t,rgbw}/<ch>` | varies                            | NOT subscribed by the library - the per-object topic is sufficient for these prefixes.                                                                                                                                                                                                                                                                                                                                                   |

## Where the method map lives

Which `AmpioClient` method drives which surface is API documentation, and it
lives on the client docstrings. The verb table in [`commands.md`](commands.md)
names the method behind each `/api` verb.
[`discovery-flow.md`](discovery-flow.md) maps the automatic bring-up sequence
against the on-demand fetches.
