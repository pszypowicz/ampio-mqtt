# Untapped surfaces

The M-SERV exposes more than the library currently consumes. Each entry
below is reachable today (the wire surface exists) but unimplemented,
and worth noting so a future contributor does not have to re-discover
it. Forward-work items each have a tracking issue; PRs picking up an
item should reference its issue number.

If you implement one of these, start by **verifying the wire shape**
against a live broker before writing any library code. The repo
already ships `tools/probe_config.py` for exactly this - it publishes
each candidate keyword on `ampio/control/<user>/config` and prints
whatever the broker replies on the matching `fromDB` topic, so the
response shape is captured without guessing:

```
python tools/probe_config.py --keywords scenes,logs,resources
```

Once the response shape is in hand, the implementation pattern is
almost always the same as `fetch_rooms()` / `fetch_locations()`:

1. Add the topic helper to `const.py`.
2. Subscribe to it in the `start()` loop.
3. Add a dispatch branch in `_dispatch` that stores the retained
   payload and sets an `asyncio.Event`.
4. Expose a `fetch_<name>()` method that publishes the request keyword
   and awaits the event.

## Reachable but not consumed

### `scenes` - the M-SERV scene catalogue

Designer's "Scenes" view is a list of named multi-action presets. The
catalogue ships over the same discovery RPC pattern as rooms
(publish `scenes` on `/data` or `/config`, await the matching
`fromDB/<user>/.../scenes` topic). A `fetch_scenes()` helper would feed
a future HA `scene` platform.

Tracked as: [#21](https://github.com/pszypowicz/ampio-mqtt/issues/21)

### `resources` - icons, media, possibly cameras

Designer's "Resources" view groups static assets (icons), media
references, and what looks like camera/intercom entries. Concrete
shape needs to be confirmed on the wire before deciding which HA
platforms this maps to.

Tracked as: [#22](https://github.com/pszypowicz/ampio-mqtt/issues/22)

### `logs` - server-side event log

The M-SERV keeps its own ring of recent events. Plausible consumers:
the HA integration's diagnostics blob (so a "broker keeps
disconnecting" report includes the broker's view of why), or a future
HA `event` platform forwarding select log lines.

Tracked as: [#23](https://github.com/pszypowicz/ampio-mqtt/issues/23)

### MD5 change-detection topics - skip redundant refetches

The M-SERV publishes content hashes on a `*/md5/*` tree so the
Designer SPA can short-circuit redundant catalogue refetches. Caching
the hashes in `AmpioClient` lets `fetch_rooms()`, `fetch_locations()`,
and future `fetch_*` helpers no-op on reconnect when nothing has
changed. Optimization, not a correctness fix - the current behaviour
of re-fetching every time is correct, just chatty.

Tracked as: [#24](https://github.com/pszypowicz/ampio-mqtt/issues/24)

### `device_raw_api` RPC bridge - per-output `outLoc` resolution

A parallel JSON-RPC-2.0-over-MQTT control channel exists alongside the
DB-object surface. The per-output **location pointer** - the integer
that points into the table returned by `fetch_locations()` - lives on
the module's CAN-resident description, not on any DB topic, and is
reachable only through this RPC. Resolving it would let the HA
integration set per-entity `suggested_area` from the Designer location
column directly.

High value, invasive: the RPC channel is its own protocol surface to
validate, and the response shape per-method is CAN-firmware specific.

Tracked as: [#25](https://github.com/pszypowicz/ampio-mqtt/issues/25)

### `symulacja` raw-channel prefix - confirm and bridge

`classify_input` routes `symulacja` to a generic-boolean `InputKind`,
but `_INPUT_CHANNEL_PREFIX` only contains the confirmed `flaga` → `f`
and `detekcja` → `i` mappings. Today `symulacja` objects still update
through the per-object topic - correct but higher-latency than the
raw form. Confirming the prefix on a live system and adding the entry
to `_INPUT_CHANNEL_PREFIX` closes the gap. See
[`raw-channel-bridge.md`](raw-channel-bridge.md).

Tracked as: [#26](https://github.com/pszypowicz/ampio-mqtt/issues/26)
