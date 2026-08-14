# Protocol surface

The M-SERV speaks two parallel topic trees on the same MQTT broker:

- **DB tree** - `ampio/fromDB/<user>/...` and `ampio/control/<user>/...`.
  User-scoped. Carries the discovery RPC pattern: publish a keyword on
  one of the control surfaces, get a JSON response on the matching
  `fromDB` topic. Per-object live state arrives on `.../ob/<id>/state`.
- **Raw tree** - `ampio/from/<MAC>/state/...`. Global, not user-scoped,
  and served only to administrator accounts (the broker ACL returns
  nothing on it for standard accounts - live-verified). Carries decoded
  CAN per-channel state, keyed by the module's effective bus MAC. Used
  as a low-latency input bridge - see
  [`raw-channel-bridge.md`](raw-channel-bridge.md).

All topic helpers live in
[`src/ampio_mqtt/const.py`](../src/ampio_mqtt/const.py). Treat the
constants there as the authoritative source; the table below is a quick
reference.

## Discovery (request / response)

Publish the keyword as the payload on the control surface. The broker
publishes the response on the corresponding `fromDB` topic. Most
responses are retained, so a fresh subscriber sees the last value
immediately.

The whole `config` surface answers only for **administrator** accounts;
standard accounts get silence there (no error, no reply - live-verified,
and independent of the account's app permissions). Everything on the
`data`, `states`, and `info` surfaces answers for every account.

| Keyword          | Control surface               | Response topic                              | Shape                                                                                                                                                     |
| ---------------- | ----------------------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `devicesDetails` | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/devicesDetails` | `{Status, List: [{id, id_urzadzenia, typ_komponentu, interpretacja, funkcja, leafId, opis_menu, stan_json, ...}]}`                                        |
| `devices`        | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/devices`        | `{List: [{id, mac, mac_global, typ_urzadzenia, nazwa_urzadzenia, wersja_softu, wersja_pcb, ...}]}`                                                        |
| `locations`      | `ampio/control/<user>/config` | `ampio/fromDB/<user>/config/locations`      | `{List: [{id, opis_menu}]}` - Designer's "Location" name table only (see [`untapped-surfaces.md`](untapped-surfaces.md) for the per-output pointer half). |
| `devices`        | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/devices`          | `{List: [...]}` - app-sync object catalogue: the `devicesDetails` row shape minus `params`/`stan_json`, filtered to the account's app grants.             |
| `params_devices` | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/params_devices`   | `{List: [{id, params, param1, czas, powiazane, url}]}` - per-object `params` bitfields for the **full** catalogue (not grant-filtered).                   |
| `groups`         | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/groups`           | `{List: [{id, id_rodzica, opis_menu}]}` - room tree.                                                                                                      |
| `group_devices`  | `ampio/control/<user>/data`   | `ampio/fromDB/<user>/data/group_devices`    | `{List: [{id_grupy, id_obiektu}]}` - object-to-room join.                                                                                                 |
| (empty)          | `ampio/control/<user>/states` | `ampio/fromDB/<user>/data/states`           | `{List: [{id, stan_json}]}` - bulk snapshot of the account's object states.                                                                               |
| (empty)          | `ampio/control/<user>/info`   | `ampio/fromDB/<user>/data/info`             | `{Results: {mac, serverVersion, serverRevision, mqttVersion, local_ip, device_id, ...}}` - server self-report; retained in the account namespace.         |

## Commands (write)

One topic per account carries every write, as plain text:

```
ampio/control/<user>/api      /api/set/<object_id>/<verb>[/<arg>...]
```

The verb vocabulary is the M-SERV's own HTTP control API re-exposed over
MQTT; its authoritative list is the OpenAPI spec embedded in the M-SERV
web app bundle (`http://<host>/assets/index-*.js`). There is no reply
topic - the object's normal state topic reports the result, typically
within ~200 ms, and an unknown verb is silently ignored.

**Commands are not grant-scoped.** The read side is filtered to the
objects an account was granted in the app, but the write side is not: any
authenticated account can command any object in the installation,
including ones it cannot see. Verified live from a non-admin account
against an object outside its grant. Treat a standard Ampio account as
full control authority over the whole installation.

The `ampio/to/<mac>/...` CAN tree is the other write path (documented in
Ampio's own MQTT API note, with per-channel `cmd` topics and a `raw` hex
channel covering CCT, DALI, blind angles, and display text). It is
**admin-only** - a non-admin account's publishes there are dropped, which
this library confirmed live - so the library uses the `/api` surface,
which works on both tiers.

| Verb                                | Args                                      | Notes                                                                                                           |
| ----------------------------------- | ----------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `turnOn`                            | -                                         | Verified. Full on (255).                                                                                        |
| `turnOff`                           | -                                         | Verified.                                                                                                       |
| `switch`                            | -                                         | Verified. Inverts current state.                                                                                |
| `open`                              | -                                         | Verified. Cover to 100.                                                                                         |
| `close`                             | -                                         | Verified. Cover to 0.                                                                                           |
| `setValue`                          | `<0-255>[/<time>]`                        | Verified. `time` is in 10 ms units and **reverts** the object afterwards - a timed pulse, not a fade.           |
| `setColors`                         | `<R>/<G>/<B>/<W>`                         | Verified. Also accepts one packed int (`R \| G<<8 \| B<<16 \| W<<24`), which is what object state reports back. |
| `setRollerPos`                      | `<position>/<lamella>`                    | Verified. Percent each; `101` on an axis means "leave it alone".                                                |
| `setColor`                          | 24-bit `R \| G<<8 \| B<<16`               | Spec-documented, not verified here.                                                                             |
| `setColorW`                         | `<rgb24>/<white>`                         | Spec-documented, not verified here.                                                                             |
| `setTemperature`, `setHeatingMode`  | regulator setpoint / mode `A`,`S`,`M`,`H` | Spec-documented, not verified here.                                                                             |
| `arm`, `disarm`                     | `<pin>`                                   | Satel alarm zones. Spec-documented, not verified here.                                                          |
| `setVolume`, `setInput`, `setSeek`  | radio module                              | Spec-documented, not verified here.                                                                             |
| `setText`                           | `<text>`                                  | Sets app-visible text on sensor objects. Spec-documented.                                                       |
| `setVirtualTemp`, `setVirtualValue` | virtual devices                           | Spec-documented, not verified here.                                                                             |

Covers stream intermediate positions in 5% steps while travelling, so a
consumer sees the movement rather than one jump to the target.

## Live state

| Topic                                      | Payload                  | Notes                                                                                                            |
| ------------------------------------------ | ------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `ampio/fromDB/<user>/ob/<id>/state`        | `{state, desc, on}`      | One per object. `state` is the value (string), `desc` is the M-SERV's pretty form, `on` is server-side ms epoch. |
| `ampio/from/<MAC>/state/f/<ch>`            | plain text (`"0"`/`"1"`) | Flag input channel; bridged to the owning object.                                                                |
| `ampio/from/<MAC>/state/i/<ch>`            | plain text (`"0"`/`"1"`) | Digital input channel; bridged to the owning object.                                                             |
| `ampio/from/<MAC>/state/{a,t,rgbw,o}/<ch>` | varies                   | NOT subscribed by the library - the per-object topic is sufficient for these prefixes.                           |

## Library helpers

| Method                          | What it does                                                                                                                                                                                                                                                                                               |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AmpioClient.start()`           | Connects, subscribes, publishes the six auto-discovery requests (`devicesDetails` and `devices` on `config`, `devices` and `params_devices` on `data`, plus the empty-payload `states` and `info` surfaces), waits for whichever catalogue answers, returns. See [`discovery-flow.md`](discovery-flow.md). |
| `AmpioClient.fetch_rooms()`     | One-shot: publishes `groups` + `group_devices`, joins both responses into `{object_id: room_name}`. On-demand because the consumer decides when room hints are needed.                                                                                                                                     |
| `AmpioClient.fetch_locations()` | One-shot: publishes `locations`, returns `{location_id: label}`. On-demand for the same reason.                                                                                                                                                                                                            |
| `AmpioClient.last_payloads`     | `{endpoint_name: payload}` of the verbatim retained response per endpoint (`details`, `devices`, `states`, `info`, `data_devices`, `params_devices`, `groups`, `group_devices`, `locations`). Intended for the HA integration's diagnostics blob.                                                          |
