# Dynamic Catalogue (0.31.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 0.31.0: `ObjectAdded` events (#79), the opt-in `refresh_interval` (#80), and the documented LAN-discovery wire facts for the Home Assistant config flow (#85), plus the events-doc delivery-context note carried over from 0.30.

**Architecture:** `ObjectAdded` subclasses `ObjectUpdated`, so the addition is invisible to existing subscriptions and a narrowing filter for new ones; the store's single object-creation site dispatches it. The periodic refresh is a client-owned task gated on availability - the reconnect path already refreshes on connect, so the timer only covers the connected steady state. The discovery work is a live probe plus documentation; no library code changes unless the probe contradicts `discovery.py`'s recorded claim.

**Tech Stack:** Python 3.13, asyncio, pytest + the FakeBroker kit, `zeroconf` (existing optional extra) for the probe.

**Spec:** Issues [#79](https://github.com/pszypowicz/ampio-mqtt/issues/79), [#80](https://github.com/pszypowicz/ampio-mqtt/issues/80), [#85](https://github.com/pszypowicz/ampio-mqtt/issues/85); the 0.30 follow-up note (the `resolve_locations()` delivery-context precedent) recorded in the 0.30 release ledger and memory.

## Global Constraints

- **`ObjectAdded` compatibility contract:** it subclasses `ObjectUpdated`. A subscription with `of=ObjectUpdated` receives additions too; `of=ObjectAdded` receives only appearances. An object's FIRST event is `ObjectAdded` - creation never also dispatches a separate `ObjectUpdated` for the same reply. Re-creation after an eviction dispatches `ObjectAdded` again. Creation dispatches even when the new row carries only defaults (a bare ghost row) - existence is the news.
- **The store has exactly one object-creation site** (`_merge_metadata`'s `obj is None` branch). The dispatch lands there and nowhere else; pending state, snapshot rows, params tables, and the designer table never create objects.
- **`refresh_interval` contract:** `None` (default) = off, exactly today's behavior. A positive float re-runs `refresh()` every interval while `available`; an offline tick skips silently (the reconnect path refreshes on connect). `start()` replaces any prior timer; `stop()` cancels it. Zero or negative raises `ValueError` at construction.
- **Proven-or-out for the discovery doc:** `docs/lan-discovery.md` records only what the live probe observed. If the probe contradicts `discovery.py`'s docstring claim (no service type, no TXT), the docstring is corrected in the same task; speculation about unobserved records is out.
- CI bars: `pytest --cov=src/ampio_mqtt --cov-branch --cov-fail-under=95`, `ruff check src tests tools`, `ruff format --check src tests tools`. Every task leaves all three green.
- Text conventions: no em dashes (single "-"), American English, no AI/tool attribution, comments and docs state durable facts only; no session-relative or edit-history narration outside the CHANGELOG.
- Public artifacts: no broker host names, no credentials, no full MAC addresses. The vendor OUI prefix (first three octets) and the mDNS hostname `ampio.local` are wire facts and fine.
- No new runtime dependencies; `zeroconf` stays the optional `[discovery]` extra.

**User decisions (already made):**

- 0.31.0 scope is #79 + #80 + #85 plus the events.md delivery-context rider; nothing else (user, 2026-08-26).
- The raw-tree docs batch (#66, #101-103) and #23 stay out of this release.
- Branch `0.31-dynamic-catalogue`; release flow as usual (squash PR, tag, pypi-approve, GitHub Release).

---

### Task 1: ObjectAdded event (#79)

**Goal:** A catalogue reply that establishes a new object dispatches `ObjectAdded` (a subclass of `ObjectUpdated`) as the object's first event; metadata changes to known objects keep dispatching plain `ObjectUpdated`.

**Files:**

- Modify: `src/ampio_mqtt/events.py`
- Modify: `src/ampio_mqtt/_store.py`
- Modify: `src/ampio_mqtt/__init__.py`
- Test: `tests/test_store.py` (append), `tests/test_events.py` (append)

**Acceptance Criteria:**

- [ ] A `devicesDetails` (and app-sync `data/devices`) reply with a new id dispatches exactly one `ObjectAdded` carrying the merged snapshot - no separate `ObjectUpdated` for the creation.
- [ ] A re-sent identical catalogue dispatches nothing; a later row change dispatches `ObjectUpdated`, not `ObjectAdded`.
- [ ] Eviction then re-listing dispatches `ObjectRemoved` then `ObjectAdded`.
- [ ] A bare row (id only, all defaults) still dispatches `ObjectAdded`.
- [ ] `client.subscribe(listener, of=ObjectUpdated)` receives additions; `of=ObjectAdded` receives only additions; `of=ObjectAdded, object_id=N` filters per object.
- [ ] `ObjectAdded` is exported from the package root.
- [ ] Coverage >= 95%, ruff clean.

**Verify:** `uv run pytest tests/test_store.py tests/test_events.py -q` -> all pass; `uv run pytest -q` -> all pass.

**Steps:**

- [ ] **Step 1: Write the failing store tests** (append to `tests/test_store.py`, using the file's existing catalogue-feeding helpers)

```python
def test_new_catalogue_row_dispatches_object_added() -> None:
    store = AmpioStore()
    applied = _apply_details(store, {"id": 7, "typ_komponentu": "flaga"})
    assert [type(e) for e in applied.events] == [ObjectAdded]
    assert applied.events[0].object.id == 7
    # The same reply again says nothing new.
    assert _apply_details(store, {"id": 7, "typ_komponentu": "flaga"}).events == []


def test_known_row_change_dispatches_updated_not_added() -> None:
    store = AmpioStore()
    _apply_details(store, {"id": 7, "typ_komponentu": "flaga"})
    applied = _apply_details(store, {"id": 7, "typ_komponentu": "flaga", "opis_menu": "x"})
    assert [type(e) for e in applied.events] == [ObjectUpdated]


def test_recreation_after_eviction_dispatches_added_again() -> None:
    store = AmpioStore()
    _apply_details(store, {"id": 7, "typ_komponentu": "flaga"})
    removed = _apply_details(store)  # empty catalogue evicts
    assert [type(e) for e in removed.events] == [ObjectRemoved]
    readded = _apply_details(store, {"id": 7, "typ_komponentu": "flaga"})
    assert [type(e) for e in readded.events] == [ObjectAdded]


def test_bare_row_creation_still_dispatches_added() -> None:
    store = AmpioStore()
    applied = _apply_details(store, {"id": 9})
    assert [type(e) for e in applied.events] == [ObjectAdded]
```

Write `_apply_details` as a small local helper if the file lacks an equivalent: it feeds a `devicesDetails` `EndpointReply` through `store.apply` and returns the `Applied` (mirror the existing helpers; Task 6 of the 0.30 plan added `_seed_catalogue`, which returns nothing - extend or add a returning variant rather than duplicating the feeding logic). Import `ObjectAdded` alongside the existing event imports.

- [ ] **Step 2: Write the failing subscription tests** (append to `tests/test_events.py`)

```python
async def test_object_added_flows_through_both_filters() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        updated: list[ObjectUpdated] = []
        added: list[ObjectAdded] = []
        client.subscribe(updated.append, of=ObjectUpdated)
        client.subscribe(added.append, of=ObjectAdded)
        feed(client, "ampio/fromDB/u/data/devices", details({"id": 5, "typ_komponentu": "flaga"}))
        assert [type(e) for e in added] == [ObjectAdded]
        # The subclass relationship keeps existing subscriptions whole.
        assert [type(e) for e in updated] == [ObjectAdded]
        feed(
            client,
            "ampio/fromDB/u/data/devices",
            details({"id": 5, "typ_komponentu": "flaga", "opis_menu": "x"}),
        )
        assert [type(e) for e in added] == [ObjectAdded]
        assert [type(e) for e in updated] == [ObjectAdded, ObjectUpdated]
    finally:
        await client.stop()


async def test_object_added_object_id_filter() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        events: list[ObjectAdded] = []
        client.subscribe(events.append, of=ObjectAdded, object_id=5)
        feed(
            client,
            "ampio/fromDB/u/data/devices",
            details({"id": 5, "typ_komponentu": "flaga"}, {"id": 6, "typ_komponentu": "flaga"}),
        )
        assert [e.object.id for e in events] == [5]
    finally:
        await client.stop()
```

Use the conftest kit (`FakeBroker`, `feed`, `details`) exactly as the file's existing tests do.

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_store.py tests/test_events.py -q`
Expected: FAIL - `ObjectAdded` does not exist.

- [ ] **Step 4: Implement the event class** (`events.py`, right after `ObjectUpdated`)

```python
@dataclass(frozen=True, slots=True)
class ObjectAdded(ObjectUpdated):
    """An object appeared in the account's catalogue.

    The object's first event: dispatched when a catalogue reply
    establishes an id the store did not hold - initial discovery, a
    Designer addition surfacing on a later reply, and the re-creation
    after an eviction all qualify (#79). A subclass of
    :class:`ObjectUpdated`, so ``of=ObjectUpdated`` subscriptions
    receive additions too; ``of=ObjectAdded`` narrows to appearances
    alone.
    """
```

Extend the store union for readability (the subclass is covered either way):

```python
StoreEvent = (
    ObjectAdded | ObjectUpdated | ObjectRemoved | ModuleUpdated | ModuleRemoved | BusEvent
)
```

- [ ] **Step 5: Dispatch from the creation site** (`_store.py`)

Import `ObjectAdded` with the other event imports. In `_merge_metadata`, capture creation and route the dispatch:

```python
        obj = self.objects.get(meta.id)
        created = obj is None
        if obj is None:
            obj = AmpioObject(id=meta.id)
```

and replace the closing dispatch block:

```python
        self.objects[meta.id] = updated
        if created:
            # Existence is the news: a bare row dispatches too, and the
            # addition is the object's first event.
            applied.events.append(ObjectAdded(updated))
        elif changed:
            self._record(updated, applied)
        return changed or created
```

- [ ] **Step 6: Export** - add `ObjectAdded` to the events import and `__all__` in `src/ampio_mqtt/__init__.py`, next to `ObjectUpdated`.

- [ ] **Step 7: Run the suite**

Run: `uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
Expected: PASS, clean. If an existing test asserts `ObjectUpdated` for a creation (search `tests/` for assertions on the first catalogue reply's events), update it to expect `ObjectAdded` - the subclass keeps `isinstance` assertions passing, so only exact-type assertions can break.

- [ ] **Step 8: Commit**

```bash
git add src/ampio_mqtt/events.py src/ampio_mqtt/_store.py src/ampio_mqtt/__init__.py tests/test_store.py tests/test_events.py
git commit -m "Dispatch ObjectAdded as an object's first event (#79)"
```

---

### Task 2: Opt-in periodic catalogue refresh (#80)

**Goal:** `AmpioClient(refresh_interval=...)` re-runs `refresh()` on a fixed cadence while connected; the default stays off.

**Files:**

- Modify: `src/ampio_mqtt/client.py`
- Test: `tests/test_lifecycle.py` (append)

**Acceptance Criteria:**

- [ ] `refresh_interval=None` (default) changes nothing: no timer task, no extra publishes.
- [ ] A positive interval re-publishes the tier's initial-discovery requests every cycle while connected.
- [ ] `stop()` cancels the cadence; nothing publishes afterwards.
- [ ] `refresh_interval=0` and negative raise `ValueError` at construction.
- [ ] An offline tick publishes nothing and the loop survives to the next tick.
- [ ] Coverage >= 95%, ruff clean.

**Verify:** `uv run pytest tests/test_lifecycle.py -q` -> all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (append to `tests/test_lifecycle.py`, using the file's existing FakeBroker patterns)

```python
async def test_refresh_interval_republishes_discovery() -> None:
    broker = FakeBroker()
    client = AmpioClient(
        "host", username="u", mqtt_client_factory=broker.factory, refresh_interval=0.05
    )
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        broker.published.clear()
        await asyncio.sleep(0.12)
        # At least one full cycle re-published the discovery set.
        assert ("ampio/control/u/states", b"") in broker.published
    finally:
        await client.stop()
    broker.published.clear()
    await asyncio.sleep(0.12)
    assert broker.published == []


async def test_refresh_interval_defaults_off() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        broker.published.clear()
        await asyncio.sleep(0.12)
        assert broker.published == []
    finally:
        await client.stop()


def test_refresh_interval_must_be_positive() -> None:
    for bad in (0, -1, -0.5):
        with pytest.raises(ValueError):
            AmpioClient("host", username="u", refresh_interval=bad)
```

For the offline-tick criterion, follow the file's existing connection-drop pattern (`broker.stream_error` / scripted publish errors): start with `refresh_interval=0.05`, force the connection down, sleep past one tick, assert no publish landed and no exception surfaced, then stop. Mirror whichever existing drop helper the file already uses rather than inventing a new one.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_lifecycle.py -q`
Expected: FAIL - unexpected keyword `refresh_interval`.

- [ ] **Step 3: Implement** (`client.py`)

Add `import contextlib` and extend the errors import to `from .errors import AmpioConnectionError, AmpioTimeoutError`. Constructor:

```python
        refresh_interval: float | None = None,
```

Docstring addition to `__init__`: "`refresh_interval` opts into a periodic re-request of the tier's discovery set, in seconds; None (the default) leaves the cadence to the consumer. Each cycle re-publishes the initial-discovery requests, so Designer additions and evictions surface as :class:`ObjectAdded` / :class:`ObjectRemoved` without a reconnect (#80)."

Validation and state, next to the `reconnect_interval` check:

```python
        if refresh_interval is not None and refresh_interval <= 0:
            raise ValueError("refresh_interval must be positive seconds or None")
        self._refresh_interval = refresh_interval
        self._refresh_task: asyncio.Task[None] | None = None
```

In `start()`, after `await self._connection.open(timeout)`:

```python
        await self._cancel_refresh_task()
        if self._refresh_interval is not None:
            self._refresh_task = asyncio.get_running_loop().create_task(
                self._refresh_periodically(self._refresh_interval)
            )
```

In `stop()`, before closing the connection:

```python
        await self._cancel_refresh_task()
```

The task and its cleanup:

```python
    async def _cancel_refresh_task(self) -> None:
        if self._refresh_task is None:
            return
        self._refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._refresh_task
        self._refresh_task = None

    async def _refresh_periodically(self, interval: float) -> None:
        """Re-run refresh() every `interval` seconds while the broker is up.

        An offline tick skips silently: the reconnect path refreshes on
        connect, so a periodic request adds nothing while the broker is
        away, and the drop that races the availability check surfaces as
        the connection error swallowed here for the same reason.
        """
        while True:
            await asyncio.sleep(interval)
            if not self.available:
                continue
            try:
                await self.refresh()
            except AmpioConnectionError:
                continue
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/ampio_mqtt/client.py tests/test_lifecycle.py
git commit -m "Add the opt-in refresh_interval cadence (#80)"
```

---

### Task 3: Live LAN-discovery probe (#85)

**Goal:** Establish, from the live network, exactly what the M-SERV publishes over mDNS (service types, TXT records, the hostname record) and the DHCP-matcher facts (vendor OUI prefix, DHCP hostname), so the discovery doc records observations, not guesses.

**USER-ORDERED GATE - NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**

- Create: `docs/superpowers/plans/2026-08-26-lan-discovery-probe.md`

**Acceptance Criteria:**

- [ ] The probe resolved `ampio.local` over mDNS and captured the address; the address matches the broker host the library connects to.
- [ ] A service-type browse (`_services._dns-sd._udp.local.` enumeration, then per-type instance browse) ran for long enough to be conclusive (>= 10 s total) and the captured output states which service types, if any, point at the M-SERV's address, with their full TXT records.
- [ ] The M-SERV's MAC was read from the ARP table after contact and ONLY its OUI prefix (first three octets) recorded, with the vendor-lookup result.
- [ ] The results file records: the A-record fact, the service-type verdict (confirming or refuting `discovery.py`'s "no service type, no TXT" claim), whether any record carries the server mac, the OUI prefix, and the observed hostname - with no full MAC, no credentials, and no non-mDNS host names.
- [ ] The probe was read-only: mDNS queries, one ping, one ARP read. No MQTT publishes at all.

**Verify:** `uv run --with zeroconf python /tmp/mdns_probe.py` -> captured service-type and TXT output, committed into the results file.

**Steps:**

- [ ] **Step 1: Write the probe script to `/tmp/mdns_probe.py`** (one-off; not committed)

```python
"""mDNS sweep: resolve ampio.local, enumerate service types, dump matching TXT."""

import socket
import time

from zeroconf import (
    AddressResolverIPv4,
    IPVersion,
    ServiceBrowser,
    ServiceInfo,
    Zeroconf,
    ZeroconfServiceTypes,
)

zc = Zeroconf()
try:
    resolver = AddressResolverIPv4("ampio.local.")
    if resolver.request(zc, 3000):
        addrs = resolver.ip_addresses_by_version(IPVersion.V4Only)
        target = str(addrs[0]) if addrs else None
    else:
        target = None
    print(f"ampio.local A record -> {target}")

    types = ZeroconfServiceTypes.find(zc=zc, timeout=6)
    print(f"service types on the LAN ({len(types)}): {sorted(types)}")

    hits = []

    class Listener:
        def add_service(self, zc_, type_, name):
            info = zc_.get_service_info(type_, name, timeout=3000)
            if info is None:
                return
            addresses = [socket.inet_ntoa(a) for a in info.addresses]
            if target in addresses:
                hits.append((type_, name, addresses, info.port, info.properties))

        def update_service(self, zc_, type_, name):
            pass

        def remove_service(self, zc_, type_, name):
            pass

    browsers = [ServiceBrowser(zc, t, Listener()) for t in types]
    time.sleep(8)
    print(f"services at the M-SERV address: {len(hits)}")
    for type_, name, addresses, port, props in hits:
        print(f"  {type_} {name} port={port} txt={props}")
finally:
    zc.close()
```

- [ ] **Step 2: Run it** (`uv run --with zeroconf python /tmp/mdns_probe.py`) and capture the output. Cross-check the resolved address against the broker host: `while IFS='=' read -r k v; do export "$k=$v"; done < ~/.config/ampio-mqtt/admin.env && python3 -c "import socket, os; print(socket.gethostbyname(os.environ['AMPIO_HOST']))"` - do not print anything else from the env file.

- [ ] **Step 3: Read the OUI**: `ping -c 1 <resolved address> >/dev/null && arp -n <resolved address>`. Record only the first three octets of the MAC plus a vendor lookup of that prefix (any public OUI table). Note the mDNS hostname (`ampio.local`) as the DHCP-hostname candidate; if the ARP output or the probe surfaced a different advertised hostname, record that spelling.

- [ ] **Step 4: Write the results file** `docs/superpowers/plans/2026-08-26-lan-discovery-probe.md`: the A-record fact, the full list of service types found on the LAN with the subset (if any) pointing at the M-SERV and their TXT records verbatim, the confirm/refute verdict on `discovery.py`'s claim, whether any record carries the server mac, the OUI prefix + vendor, and the hostname facts. Sanitize per the acceptance criteria.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-08-26-lan-discovery-probe.md
git commit -m "Probe the M-SERV's mDNS and DHCP discovery surface (#85)"
```

---

### Task 4: Docs, CHANGELOG, version 0.31.0

**Goal:** The discovery facts, the new event and cadence contracts, and the 0.30 delivery-context note land in the docs; the CHANGELOG and version close the release.

**Files:**

- Create: `docs/lan-discovery.md`
- Modify: `docs/events.md`, `docs/discovery-flow.md`, `docs/README.md`, `CHANGELOG.md`, `src/ampio_mqtt/__init__.py`
- Modify (conditional): `src/ampio_mqtt/discovery.py` (only if Task 3 refuted its docstring claim)

**Acceptance Criteria:**

- [ ] `docs/lan-discovery.md` states, from the Task 3 results only: what the M-SERV publishes over mDNS, whether HA's manifest zeroconf matcher has anything to match, the DHCP-matcher facts (OUI prefix, hostname), that no record carries the server mac if that is what the probe found - and therefore that the config flow obtains its unique id by probing the broker (`test_connection`, `AmpioServerInfo.key`), plus the `discover()` fallback for a manual flow.
- [ ] `docs/events.md` documents `ObjectAdded` (first-event contract, the subclass relationship, the ordering with `ObjectRemoved` on re-creation) and gains the delivery-context paragraph: events dispatch synchronously on the loop that ran `start()`; most arrive from the connection task, and an explicit call like `resolve_locations()` dispatches from the caller's task - same loop, same guarantees.
- [ ] `docs/discovery-flow.md` documents `refresh_interval` next to `refresh()`.
- [ ] `docs/README.md` indexes the new page.
- [ ] `CHANGELOG.md` gains `## 0.31.0`: Added (`ObjectAdded` (#79), `refresh_interval` (#80), `docs/lan-discovery.md` (#85)); the entry states the compatibility note (additions now dispatch `ObjectAdded`; `of=ObjectUpdated` subscriptions receive them unchanged because it subclasses).
- [ ] `__version__ = "0.31.0"`.
- [ ] `uv run pytest -q`, `ruff check`, `ruff format --check` all green.

**Verify:** `uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools && grep __version__ src/ampio_mqtt/__init__.py` -> green, `0.31.0`.

**Steps:**

- [ ] **Step 1: Write `docs/lan-discovery.md`** from the Task 3 results file, in the docs' established voice; index it in `docs/README.md`.
- [ ] **Step 2: Update `docs/events.md`** (the `ObjectAdded` contract in "What arrives", the re-creation ordering in "Ordering", and the delivery-context paragraph).
- [ ] **Step 3: Update `docs/discovery-flow.md`** (the `refresh_interval` cadence next to the `refresh()` description).
- [ ] **Step 4: If Task 3 refuted the `discovery.py` docstring claim, correct that docstring; otherwise leave the file untouched.**
- [ ] **Step 5: CHANGELOG `## 0.31.0` + version bump.**
- [ ] **Step 6: Run the full suite and commit**

```bash
git add docs CHANGELOG.md src/ampio_mqtt/__init__.py
git commit -m "0.31.0: ObjectAdded, refresh_interval, LAN discovery facts (#79, #80, #85)"
```

---

## Self-review notes

- Names consistent across tasks: `ObjectAdded`, `refresh_interval`, `_refresh_periodically`, `_cancel_refresh_task`, `docs/lan-discovery.md`.
- Tasks 1, 2, 3 are mutually independent (disjoint files); Task 4 depends on all three.
- Task 1 Step 7 names the one migration hazard: exact-type assertions on creation events in the existing suite.
- The release itself (PR, tag, PyPI approval, GitHub Release) follows the usual flow after the plan completes; it is not a plan task.
