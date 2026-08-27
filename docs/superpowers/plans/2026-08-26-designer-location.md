# Designer Location (AmpioObject.location) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers-extended-cc:subagent-driven-development (recommended) or
> superpowers-extended-cc:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 0.30.0: `AmpioObject.location` resolved from the per-output
Designer location, via the `device_api` get_data surface, with the locations
name table restored and `matter_device_type` refined from the same record.

**Architecture:** A new admin-only request/reply pair
(`device_api/to/<machex>/get_data` -> `device_api/from/<MACHEX>/info`) delivers
each module's CAN-resident description record. A pure join in `_protocol.py`
maps objects to entries through `(descType, out-no)`. The store keeps the
resolved table and re-applies it on every catalogue merge, exactly as it keeps
`params_devices`.

**Tech Stack:** Python 3.13, asyncio, aiomqtt (existing floor), pytest + the
FakeBroker kit in `tests/conftest.py`, the `.verify-harness/` local mosquitto
fixtures.

**Spec:** Issue [#25](https://github.com/pszypowicz/ampio-mqtt/issues/25)
(retitled; its 2026-08-25 comment holds the corrected wire contract), issue
[#110](https://github.com/pszypowicz/ampio-mqtt/issues/110), and the "Wire
contract" section below.

## Global Constraints

- **Wire contract** (live-proven 2026-08-25, two protocol-23 modules):
  - Request: publish empty payload to `device_api/to/<machex>/get_data`, mac in
    lowercase hex.
  - Reply: JSON on `device_api/from/<MACHEX>/info` (mac in UPPERCASE hex - parse
    the topic segment with `int(x, 16)`, never compare strings). Key field:
    `descriptions`, base64.
  - Blob: repeated little-endian frames
    `[len:2][descType:2][outNo:2][outLoc:2][outType:2][utf8 desc]`. `len` counts
    the whole frame. `len < 10` ends the walk.
  - `outLoc` points into the locations name table: request keyword `locations`
    on `ampio/control/admin/config`, reply
    `{"List":[{"id", "opis_menu", "opis_rozwiniety"}]}` on
    `ampio/fromDB/admin/config/locations`. `outLoc` 0 = unassigned.
  - `outType` is the Matter device type (256 = 0x0100). 0 = untagged. The CAN
    value is authoritative; the DB `type` column mirror lags it (#110).
  - descType enum (Designer bundle): DEVICE_NAME=1, OW=3, FLAG_BIN=6, FLAG_U8=7,
    FLAG_I16=8, INPUTS=10, OUTPUTS=12, IN_U8=14, MLED=15, OUT_OC_U8=16, MRT=17,
    SCREEN_NO=20, FLAG_BIN_SIMPLE=22, SatelZone=23, SatelInput=24,
    SatelOutput=25, ROLLER=26.
  - The whole `device_api` tree is admin-only. The restricted account gets
    silence on subscribe and request.
  - Proven join pairs: `przekaznik` -> descType 12 (OUTPUTS), `roleta_procenty`
    -> descType 26 (ROLLER); out-no = the last `leafId` segment. Task 1 proves
    or refutes more pairs; **only proven pairs ship** (`DESC_TYPE_BY_KIND`).
- **Proven-or-out:** wire behavior lands only with live proof. No speculative
  descType pairs, no defensive branches for shapes never observed.
- No new runtime dependencies. `base64`/`binascii` are stdlib.
- CI bars: `pytest --cov=src/ampio_mqtt --cov-branch --cov-fail-under=95`,
  `ruff check src tests tools`, `ruff format --check`. Every task leaves all
  three green.
- Comment style: durable invariants only - no session references, no "changed
  from X", no coverage numbers (repo CLAUDE conventions).
- Public artifacts (commits, PR): no host names, no room names, no device names,
  no macs from the live install. `cb89`-style macs stay in tests/fixtures only
  as synthetic values.

**User decisions (already made):**

- The API freeze (#86) happens AFTER the merge to Home Assistant core, not
  before - shipping new surface in 0.30.0 is sanctioned (user, 2026-08-26).
- No new tracking issue: #25 is the tracker (retitled), #110 rides along in the
  same release.
- Branch `0.30-designer-location`; the release is the next minor, 0.30.0.
- Approved API surface: `AmpioObject.location: str | None`, restored
  `AmpioClient.fetch_locations()`, new admin-only
  `AmpioClient.resolve_locations()` sweep; `matter_device_type` refined from CAN
  `outType` (never cleared by it).
- Consumer contract (HA integration, later): "Designer location wins, app room
  as fallback" - the library ships the location value; the precedence rule lives
  in the consumer.

---

### Task 1: Prove the join rule live across the full catalogue

**Goal:** Prove, on the real install, which `typ_komponentu` -> descType pairs
hold and that the last `leafId` segment (not `funkcja`) is the out-no join key,
across all modules - the result fixes `DESC_TYPE_BY_KIND` for Task 3.

**USER-ORDERED GATE - NON-SKIPPABLE.** This task was requested by the user in
the current conversation. It MUST NOT be closed by walking around it, by
declaring it "verified inline", or by substituting a cheaper check. Close only
after every item in `acceptanceCriteria` has been re-validated independently,
with output captured.

**Files:**

- Create: `docs/superpowers/plans/2026-08-26-join-proof-results.md` (the
  captured evidence)

**Acceptance Criteria:**

- [ ] The probe ran against the live broker with the admin account and captured
      replies from >= 30 of the 39 modules (offline modules are tolerated and
      listed).
- [ ] For every object with a non-empty `leafId` on an answering module, the
      report states whether `(candidate descType, last leafId segment)` matched
      an entry, and whether `funkcja` would have matched instead.
- [ ] A results file records: the winning out-no key, the proven
      `typ_komponentu -> descType` table with match counts, and the kinds left
      unproven.
- [ ] Read access only: the probe publishes only `get_data`, `devicesDetails`,
      `devices`, and `locations` requests. No `/api/set`, no writes.

**Verify:** `uv run --with aiomqtt python /tmp/join_proof.py` -> a per-kind
table with match ratios, committed into the results file.

**Steps:**

- [ ] **Step 1: Write the probe script to `/tmp/join_proof.py`** (one-off; not
      committed)

```python
"""Join-rule proof: get_data every module, join objects to description entries.

Reads AMPIO_HOST/AMPIO_USERNAME/AMPIO_PASSWORD (load ~/.config/ampio-mqtt/admin.env
line by line - do not source it). Read-only: publishes only request keywords.
"""

import asyncio
import base64
import json
import os
from collections import defaultdict

import aiomqtt

DESC_ENUM = {1: "DEVICE_NAME", 3: "OW", 6: "FLAG_BIN", 7: "FLAG_U8", 8: "FLAG_I16",
             10: "INPUTS", 12: "OUTPUTS", 14: "IN_U8", 15: "MLED", 16: "OUT_OC_U8",
             17: "MRT", 20: "SCREEN_NO", 22: "FLAG_BIN_SIMPLE", 23: "SatelZone",
             24: "SatelInput", 25: "SatelOutput", 26: "ROLLER"}


def decode(blob: bytes):
    out, off = [], 0
    while off + 10 <= len(blob):
        ln = int.from_bytes(blob[off:off + 2], "little")
        if ln < 10 or off + ln > len(blob):
            break
        out.append({
            "descType": int.from_bytes(blob[off + 2:off + 4], "little"),
            "outNo": int.from_bytes(blob[off + 4:off + 6], "little"),
            "outLoc": int.from_bytes(blob[off + 6:off + 8], "little"),
            "outType": int.from_bytes(blob[off + 8:off + 10], "little"),
            "desc": blob[off + 10:off + ln].decode("utf-8", "replace"),
        })
        off += ln
    return out


async def main() -> None:
    host, user, pw = (os.environ[k] for k in
                      ("AMPIO_HOST", "AMPIO_USERNAME", "AMPIO_PASSWORD"))
    objects, modules, by_mac = [], [], {}
    async with aiomqtt.Client(host, username=user, password=pw) as c:
        await c.subscribe(f"ampio/fromDB/{user}/config/#", qos=1)
        await c.subscribe("device_api/from/+/info", qos=1)
        await c.publish(f"ampio/control/{user}/config", "devicesDetails")
        await c.publish(f"ampio/control/{user}/config", "devices")

        async def collect():
            async for msg in c.messages:
                t = str(msg.topic)
                if t.endswith("config/devicesDetails"):
                    objects[:] = json.loads(msg.payload)["List"]
                elif t.endswith("config/devices"):
                    modules[:] = json.loads(msg.payload)["List"]
                    for m in modules:
                        await c.publish(
                            f"device_api/to/{m['mac']:x}/get_data", "")
                elif t.startswith("device_api/from/"):
                    mac = int(t.split("/")[2], 16)
                    rec = json.loads(msg.payload)
                    by_mac[mac] = decode(
                        base64.b64decode(rec.get("descriptions") or ""))

        try:
            await asyncio.wait_for(collect(), timeout=45)
        except TimeoutError:
            pass

    answered = set(by_mac)
    silent = [m["mac"] for m in modules if m["mac"] not in answered]
    print(f"modules answered: {len(answered)}/{len(modules)}; silent: "
          f"{sorted(silent)}")
    stats: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "leaf_hits": defaultdict(int), "funkcja_hits": 0})
    for o in objects:
        leaf = o.get("leafId") or ""
        parts = leaf.split("_")
        if len(parts) != 5:
            continue
        mac, out_no = int(parts[1], 16), parts[4]
        if mac not in by_mac or not out_no.isdigit():
            continue
        typ = o.get("typ_komponentu") or "?"
        s = stats[typ]
        s["total"] += 1
        for e in by_mac[mac]:
            if e["outNo"] == int(out_no):
                s["leaf_hits"][e["descType"]] += 1
            if o.get("funkcja") is not None and e["outNo"] == o["funkcja"]:
                s["funkcja_hits"] += 1
    for typ, s in sorted(stats.items()):
        hits = {DESC_ENUM.get(k, k): v for k, v in s["leaf_hits"].items()}
        print(f"{typ:20s} total={s['total']:3d} leaf-key hits by descType="
              f"{dict(hits)} funkcja-key hits={s['funkcja_hits']}")


asyncio.run(main())
```

- [ ] **Step 2: Run it with the admin env** (load the env file line by line;
      never `source` it, never echo the password)

Run:
`while IFS='=' read -r k v; do export "$k=$v"; done < ~/.config/ampio-mqtt/admin.env && uv run --with aiomqtt python /tmp/join_proof.py`
Expected: an answered-modules line and one row per `typ_komponentu`. A kind is
PROVEN for descType D when its leaf-key hits land on exactly one descType and
cover a clear majority of `total` (missing hits must be explainable as modules
that never got a description written).

- [ ] **Step 3: Record the results file**

Write `docs/superpowers/plans/2026-08-26-join-proof-results.md` with: the
answered/silent counts, the winning out-no key (leaf segment vs `funkcja`, with
the hit counts), the proven pairs table (`typ_komponentu` | descType |
hits/total), the unproven kinds, and the decision line "DESC_TYPE_BY_KIND ships
exactly these pairs: ...". Do not include room names, device names, or the host.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-08-26-join-proof-results.md
git commit -m "Prove the description join rule on the live catalogue (#25)"
```

---

### Task 2: Restore the locations name table (fetch_locations)

**Goal:** `AmpioClient.fetch_locations()` returns `{location_id: name}` again,
as an admin-only on-demand endpoint, and `_fetch` rejects a non-served endpoint
with a clear error.

**Files:**

- Modify: `src/ampio_mqtt/_protocol.py` (add `parse_locations`, add the
  `locations` endpoint row)
- Modify: `src/ampio_mqtt/client.py` (add `fetch_locations`, guard `_fetch`)
- Create: `tests/test_locations.py`

**Acceptance Criteria:**

- [ ] `parse_locations` returns `{id: opis_menu}`, skips malformed rows, returns
      None for a non-List payload.
- [ ] `fetch_locations()` publishes `locations` to `ampio/control/admin/config`
      and returns the parsed reply from `ampio/fromDB/admin/config/locations`.
- [ ] On the restricted tier, `fetch_locations()` raises `RuntimeError` naming
      the tier - not `KeyError`.
- [ ] Coverage stays >= 95%, ruff clean.

**Verify:** `uv run pytest tests/test_locations.py -q` -> all pass;
`uv run pytest -q` -> all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (`tests/test_locations.py`)

```python
"""Tests for the Designer locations name table (config/locations)."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import ADMIN_USER, FakeBroker, deliver_later

from ampio_mqtt import AmpioClient
from ampio_mqtt._protocol import parse_locations

LOCATIONS_TOPIC = f"ampio/fromDB/{ADMIN_USER}/config/locations"


def test_parse_locations_happy_path() -> None:
    payload = json.dumps(
        {"List": [{"id": 14, "opis_menu": "Potter"}, {"id": 19, "opis_menu": "Testowe"}]}
    )
    assert parse_locations(payload) == {14: "Potter", 19: "Testowe"}


def test_parse_locations_skips_malformed_rows() -> None:
    payload = json.dumps(
        {
            "List": [
                {"id": 1, "opis_menu": "OK"},
                {"id": None, "opis_menu": "x"},
                {"id": 2, "opis_menu": ""},
                "not a dict",
            ]
        }
    )
    assert parse_locations(payload) == {1: "OK"}


def test_parse_locations_rejects_non_list_payload() -> None:
    assert parse_locations("not-json") is None
    assert parse_locations(json.dumps({"Status": 0})) is None


async def test_fetch_locations_requests_and_parses() -> None:
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.01)
    broker.published.clear()
    try:
        delivery = deliver_later(
            client,
            (LOCATIONS_TOPIC, json.dumps({"List": [{"id": 14, "opis_menu": "Potter"}]})),
        )
        try:
            result = await client.fetch_locations(timeout=1.0)
        finally:
            await delivery
        assert result == {14: "Potter"}
        assert (f"ampio/control/{ADMIN_USER}/config", b"locations") in broker.published
    finally:
        await client.stop()


async def test_fetch_locations_raises_on_restricted_tier() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        with pytest.raises(RuntimeError, match="restricted"):
            await client.fetch_locations(timeout=0.1)
    finally:
        await client.stop()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_locations.py -q` Expected: FAIL -
`parse_locations` does not exist.

- [ ] **Step 3: Add the parser and the endpoint row** (`_protocol.py`)

Place `parse_locations` next to `parse_rooms`:

```python
def parse_locations(payload: str) -> dict[int, str] | None:
    """``{location_id: name}`` from a `config/locations` reply.

    The name table behind the Designer's "Lokalizacja" dropdown; rows with
    a missing id or an empty name are skipped. None when the payload is
    not a ``{"List": [...]}`` document.
    """
    rows = list_rows(payload)
    if rows is None:
        return None
    out: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        lid = to_int(row.get("id"))
        name = row.get("opis_menu")
        if lid is not None and isinstance(name, str) and name:
            out[lid] = name
    return out
```

Append to `ENDPOINTS` (after the `scenes` row):

```python
    # The Designer "Lokalizacja" name table. On-demand; the per-output
    # pointer that resolves through it rides the device_api record
    # (resolve_locations()).
    Endpoint(
        "locations",
        "config",
        "locations",
        "config",
        "locations",
        tier=AccessTier.ADMIN,
        parses=parse_locations,
    ),
```

- [ ] **Step 4: Add `fetch_locations` and the `_fetch` tier guard**
      (`client.py`)

In `_fetch`, extend the pre-flight loop (the guard runs before the `parses`
check):

```python
        for name in names:
            if name not in self._channels:
                raise RuntimeError(
                    f"endpoint {name!r} is not served on the "
                    f"{self._tier.value} tier"
                )
            if ENDPOINT_BY_NAME[name].parses is None:
```

Add the method next to `fetch_scenes`:

```python
    async def fetch_locations(self, timeout: float = 5.0) -> dict[int, str]:
        """Return ``{location_id: name}`` - the Designer "Lokalizacja" table.

        The name table the per-output location pointer resolves through;
        :meth:`resolve_locations` consumes it and per-object consumers read
        :pyattr:`AmpioObject.location` instead. Admin tier only - the
        ``config`` surface never answers a restricted account, and the call
        raises ``RuntimeError`` for one. Raises ``AmpioConnectionError`` if
        the broker is not connected and ``AmpioTimeoutError`` if the
        response does not arrive within ``timeout``.
        """
        replies = await self._fetch(
            ("locations",),
            timeout,
            "Timed out fetching the locations table from the Ampio broker",
        )
        return dict(cast("dict[int, str]", replies["locations"]))
```

- [ ] **Step 5: Run the suite**

Run:
`uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add src/ampio_mqtt/_protocol.py src/ampio_mqtt/client.py tests/test_locations.py
git commit -m "Restore the Designer locations name table as an admin endpoint (#25)"
```

---

### Task 3: Descriptions wire layer (parsers, router, topics)

**Goal:** `_protocol.py` decodes a `device_api/from/<MAC>/info` reply into typed
`OutputDescription` entries and routes it; the topic helpers and the proven
`DESC_TYPE_BY_KIND` constants exist.

**Files:**

- Modify: `src/ampio_mqtt/_protocol.py`
- Create: `tests/test_descriptions.py`

**Acceptance Criteria:**

- [ ] `parse_descriptions_blob` decodes the little-endian frames and stops on a
      short or overrunning length.
- [ ] `parse_device_info` returns `()` for a record without `descriptions`, None
      for non-JSON or bad base64.
- [ ] The router turns `device_api/from/CB89/info` into
      `DeviceDescriptions(mac=0xCB89, entries=...)`, case-insensitively, and
      returns None for an unparseable payload.
- [ ] `DESC_TYPE_BY_KIND` contains exactly the pairs Task 1 proved (at minimum
      `przekaznik: 12` and `roleta_procenty: 26`, both already live-proven).
- [ ] Coverage >= 95%, ruff clean.

**Verify:** `uv run pytest tests/test_descriptions.py -q` -> all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (`tests/test_descriptions.py`)

```python
"""Tests for the device_api descriptions wire layer."""

from __future__ import annotations

import base64
import json

from ampio_mqtt._protocol import (
    OutputDescription,
    DeviceDescriptions,
    Router,
    ENDPOINTS,
    device_api_request_topic,
    parse_descriptions_blob,
    parse_device_info,
)


def frame(desc_type: int, out_no: int, out_loc: int, out_type: int, desc: str) -> bytes:
    body = desc.encode()
    length = 10 + len(body)
    return b"".join(
        v.to_bytes(2, "little") for v in (length, desc_type, out_no, out_loc, out_type)
    ) + body


def test_blob_decodes_frames_in_order() -> None:
    blob = frame(12, 0, 14, 256, "Lampa") + frame(26, 1, 19, 514, "Roleta")
    assert parse_descriptions_blob(blob) == (
        OutputDescription(desc_type=12, out_no=0, out_loc=14, out_type=256, desc="Lampa"),
        OutputDescription(desc_type=26, out_no=1, out_loc=19, out_type=514, desc="Roleta"),
    )


def test_blob_stops_on_short_or_overrunning_length() -> None:
    assert parse_descriptions_blob(frame(12, 0, 0, 0, "ok") + b"\x02\x00") == (
        OutputDescription(desc_type=12, out_no=0, out_loc=0, out_type=0, desc="ok"),
    )
    truncated = frame(12, 0, 0, 0, "long description")[:-4]
    assert parse_descriptions_blob(truncated) == ()


def test_device_info_extracts_descriptions() -> None:
    payload = json.dumps(
        {"macProd": 52105, "descriptions": base64.b64encode(frame(12, 0, 14, 256, "L")).decode()}
    )
    entries = parse_device_info(payload)
    assert entries is not None and entries[0].out_loc == 14


def test_device_info_without_descriptions_reads_empty() -> None:
    assert parse_device_info(json.dumps({"macProd": 1})) == ()
    assert parse_device_info(json.dumps({"descriptions": ""})) == ()


def test_device_info_rejects_garbage() -> None:
    assert parse_device_info("not-json") is None
    assert parse_device_info(json.dumps({"descriptions": "!!!not-base64"})) is None
    assert parse_device_info(json.dumps({"descriptions": 5})) is None


def test_router_routes_info_reply_case_insensitively() -> None:
    router = Router("admin", ENDPOINTS)
    payload = json.dumps(
        {"descriptions": base64.b64encode(frame(12, 2, 3, 0, "x")).decode()}
    )
    msg = router.route("device_api/from/CB89/info", payload)
    assert isinstance(msg, DeviceDescriptions)
    assert msg.mac == 0xCB89
    assert msg.entries[0].out_no == 2
    assert router.route("device_api/from/zz/info", payload) is None
    assert router.route("device_api/from/CB89/info", "not-json") is None


def test_request_topic_uses_lowercase_hex() -> None:
    assert device_api_request_topic(0xCB89) == "device_api/to/cb89/get_data"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_descriptions.py -q` Expected: FAIL - imports
missing.

- [ ] **Step 3: Implement in `_protocol.py`**

Add `import base64` and `import binascii` to the module imports. Add the
dataclass and parsers next to the other parsers:

```python
@dataclass(slots=True, frozen=True)
class OutputDescription:
    """One per-output entry of a module's CAN-resident description record."""

    desc_type: int  # description class (OUTPUTS=12, ROLLER=26, ...)
    out_no: int  # output index within the class
    out_loc: int  # pointer into the locations name table; 0 = unassigned
    out_type: int  # Matter device type; 0 = untagged
    desc: str


def parse_descriptions_blob(blob: bytes) -> tuple[OutputDescription, ...]:
    """Decode the flat description frames.

    ``[len:2][descType:2][outNo:2][outLoc:2][outType:2][utf8 desc]``,
    little-endian, repeated; ``len`` counts the whole frame. A length
    below the 10-byte header or past the end stops the walk - the
    remainder is unreadable either way.
    """
    out: list[OutputDescription] = []
    offset = 0
    while offset + 10 <= len(blob):
        length = int.from_bytes(blob[offset : offset + 2], "little")
        if length < 10 or offset + length > len(blob):
            break
        out.append(
            OutputDescription(
                desc_type=int.from_bytes(blob[offset + 2 : offset + 4], "little"),
                out_no=int.from_bytes(blob[offset + 4 : offset + 6], "little"),
                out_loc=int.from_bytes(blob[offset + 6 : offset + 8], "little"),
                out_type=int.from_bytes(blob[offset + 8 : offset + 10], "little"),
                desc=blob[offset + 10 : offset + length].decode("utf-8", "replace"),
            )
        )
        offset += length
    return tuple(out)


def parse_device_info(payload: str) -> tuple[OutputDescription, ...] | None:
    """The description entries of a ``device_api/from/<mac>/info`` reply.

    A record without a ``descriptions`` field reads as empty - a module
    with no descriptions written. None when the payload is not a JSON
    object or the base64 is unreadable.
    """
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("descriptions")
    if raw in (None, ""):
        return ()
    if not isinstance(raw, str):
        return None
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return None
    return parse_descriptions_blob(blob)
```

Add the constants and topic helpers next to the raw-tree wildcards:

```python
# The admin-only device_api tree: get_data asks the M-SERV for a module's
# full CAN-resident record; the info reply carries the description
# entries. Request macs are lowercase hex, reply macs uppercase - the
# router parses the segment numerically.
DEVICE_API_INFO_WILDCARD = "device_api/from/+/info"


def device_api_request_topic(mac: int) -> str:
    """The get_data request topic for one module's CAN-resident record."""
    return f"device_api/to/{mac:x}/get_data"


# typ_komponentu -> description class (descType), live-proven pairs only
# (docs/identity.md): an unlisted kind resolves no location. Extend only
# with a live-proven pair.
DESC_TYPE_BY_KIND: dict[str, int] = {
    "przekaznik": 12,  # OUTPUTS
    "roleta_procenty": 26,  # ROLLER
    # Task 1 (join-proof) pairs land here.
}
```

Add the inbound type next to `DiagnosticsReport`, extend the `Inbound` union,
and route it in `Router.route` (insert before the `ampio/from` branch):

```python
@dataclass(slots=True, frozen=True)
class DeviceDescriptions:
    """A module's parsed description record from a device_api info reply."""

    mac: int
    entries: tuple[OutputDescription, ...]
```

```python
Inbound = (
    EndpointReply
    | StateUpdate
    | RawChannelEdge
    | DiagnosticsReport
    | DeviceDescriptions
    | BusEvent
)
```

```python
        if (
            len(parts) == 4
            and parts[0] == "device_api"
            and parts[1] == "from"
            and parts[3] == "info"
        ):
            try:
                mac = int(parts[2], 16)
            except ValueError:
                return None
            entries = parse_device_info(payload)
            return None if entries is None else DeviceDescriptions(mac=mac, entries=entries)
```

- [ ] **Step 4: Extend `DESC_TYPE_BY_KIND` with the Task 1 proven pairs** (read
      `2026-08-26-join-proof-results.md`; add exactly those pairs, each with the
      enum-name comment)

- [ ] **Step 5: Run the suite**

Run:
`uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
Expected: PASS, clean.

- [ ] **Step 6: Commit**

```bash
git add src/ampio_mqtt/_protocol.py tests/test_descriptions.py
git commit -m "Decode and route the device_api description record (#25)"
```

---

### Task 4: AmpioObject.location field and leaf out-no

**Goal:** `AmpioObject` carries `location: str | None` and exposes `leaf_out_no`
parsed from the last `leafId` segment.

**Files:**

- Modify: `src/ampio_mqtt/models.py`
- Test: `tests/test_models.py` (append)

**Acceptance Criteria:**

- [ ] `AmpioObject(id=1).location is None`; the field survives
      `dataclasses.replace` of other fields.
- [ ] `leaf_out_no` returns the last segment as int (`0_cb89_257_2_7` -> 7),
      None for an empty or malformed `leaf_id` and for a non-numeric segment.
- [ ] `module_mac` behavior is unchanged by the regex edit (existing tests stay
      green).

**Verify:** `uv run pytest tests/test_models.py -q` -> all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (append to `tests/test_models.py`)

```python
def test_location_defaults_to_none_and_survives_replace() -> None:
    obj = AmpioObject(id=1, location="Potter")
    assert AmpioObject(id=1).location is None
    assert replace(obj, value="1").location == "Potter"


def test_leaf_out_no_parses_last_segment() -> None:
    assert AmpioObject(id=1, leaf_id="0_cb89_257_2_7").leaf_out_no == 7
    assert AmpioObject(id=1, leaf_id="0_cb89_257_2_0").leaf_out_no == 0
    assert AmpioObject(id=1, leaf_id="").leaf_out_no is None
    assert AmpioObject(id=1, leaf_id="0_cb89_257_2_x").leaf_out_no is None
    assert AmpioObject(id=1, leaf_id="junk").leaf_out_no is None
```

(Use the module's existing imports; add `from dataclasses import replace` if
absent.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_models.py -q` Expected: FAIL - unexpected keyword
`location`.

- [ ] **Step 3: Implement** (`models.py`)

Extend the regex (second capture group; the mac group and `module_mac` stay as
they are):

```python
_LEAF_ID_RE = re.compile(r"0_([0-9a-fA-F]+)_[^_]+_[^_]+_([^_]+)")
```

Add the field after `matter_device_type`:

```python
    # Designer per-output location name (the "Lokalizacja" dropdown),
    # resolved from the module's CAN-resident description record by
    # AmpioClient.resolve_locations() - admin tier only. None until a
    # resolve ran, and for objects it could not match. docs/identity.md.
    location: str | None = None
```

Add the property next to `module_mac`:

```python
    @property
    def leaf_out_no(self) -> int | None:
        """The output index within the module's description record.

        Parsed from ``leaf_id``'s last segment - the join key that pairs
        this object with its :class:`OutputDescription` entry
        (docs/identity.md). None when ``leaf_id`` is empty, malformed,
        or the segment is not a number.
        """
        match = _LEAF_ID_RE.fullmatch(self.leaf_id)
        if match is None:
            return None
        try:
            return int(match.group(2))
        except ValueError:
            return None
```

- [ ] **Step 4: Run the suite**

Run:
`uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/ampio_mqtt/models.py tests/test_models.py
git commit -m "Add AmpioObject.location and the leaf out-no join key (#25)"
```

---

### Task 5: The pure join (resolve_designer)

**Goal:** A pure `_protocol.resolve_designer()` maps objects to
`DesignerResolution(location, matter_device_type)` through `(descType, out-no)`,
skipping colliding macs and unproven kinds.

**Files:**

- Modify: `src/ampio_mqtt/_protocol.py`
- Test: `tests/test_descriptions.py` (append)

**Acceptance Criteria:**

- [ ] A relay on mac 0xCB89 with `leaf_id` ending `_0` joins the `(12, 0)`
      entry; `out_loc` resolves through the names map; `out_loc` 0 yields
      `location=None`; `out_type` 0 yields `matter_device_type=None`.
- [ ] Objects with an unproven kind, an empty `leaf_id`, an unanswered module,
      or a colliding mac produce no resolution.
- [ ] Coverage >= 95%, ruff clean.

**Verify:** `uv run pytest tests/test_descriptions.py -q` -> all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (append to
      `tests/test_descriptions.py`)

```python
from ampio_mqtt._protocol import DesignerResolution, resolve_designer
from ampio_mqtt.models import AmpioObject


def _entries(*specs: tuple[int, int, int, int, str]) -> tuple[OutputDescription, ...]:
    return tuple(OutputDescription(*s) for s in specs)


def test_resolve_designer_joins_location_and_type() -> None:
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
        48: AmpioObject(id=48, typ_komponentu="roleta_procenty", leaf_id="0_cb89_5_0_1"),
    }
    by_mac = {
        0xCB89: _entries((12, 0, 14, 256, "Lampa"), (26, 1, 0, 0, "Roleta")),
    }
    resolved = resolve_designer(objects, by_mac, {14: "Potter"}, frozenset())
    assert resolved == {
        64: DesignerResolution(location="Potter", matter_device_type=256),
        48: DesignerResolution(location=None, matter_device_type=None),
    }


def test_resolve_designer_skips_the_unjoinable() -> None:
    objects = {
        1: AmpioObject(id=1, typ_komponentu="flaga_x", leaf_id="0_cb89_3_0_0"),
        2: AmpioObject(id=2, typ_komponentu="przekaznik", leaf_id=""),
        3: AmpioObject(id=3, typ_komponentu="przekaznik", leaf_id="0_beef_257_2_0"),
        4: AmpioObject(id=4, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_9"),
    }
    by_mac = {0xCB89: _entries((12, 0, 14, 256, "L"))}
    assert resolve_designer(objects, by_mac, {14: "P"}, frozenset()) == {}


def test_resolve_designer_skips_colliding_macs() -> None:
    objects = {
        64: AmpioObject(id=64, typ_komponentu="przekaznik", leaf_id="0_cb89_257_2_0"),
    }
    by_mac = {0xCB89: _entries((12, 0, 14, 256, "L"))}
    assert resolve_designer(objects, by_mac, {14: "P"}, frozenset({0xCB89})) == {}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_descriptions.py -q` Expected: FAIL -
`resolve_designer` does not exist.

- [ ] **Step 3: Implement** (`_protocol.py`; add `Mapping` to the
      `collections.abc` import and `AmpioObject` to the models import)

```python
@dataclass(slots=True, frozen=True)
class DesignerResolution:
    """What one object's CAN description entry proves."""

    location: str | None
    matter_device_type: int | None


def resolve_designer(
    objects: Mapping[int, AmpioObject],
    descriptions_by_mac: Mapping[int, tuple[OutputDescription, ...]],
    location_names: Mapping[int, str],
    colliding_macs: frozenset[int],
) -> dict[int, DesignerResolution]:
    """Join each object to its module's description entry.

    The key is ``(DESC_TYPE_BY_KIND[typ_komponentu], leaf_out_no)`` within
    the module record of ``module_mac``. Objects on a colliding mac are
    skipped - the reply cannot be attributed to one module. ``out_loc`` 0
    reads unassigned and ``out_type`` 0 untagged, so neither produces a
    value.
    """
    entries_by_key = {
        mac: {(e.desc_type, e.out_no): e for e in entries}
        for mac, entries in descriptions_by_mac.items()
    }
    out: dict[int, DesignerResolution] = {}
    for obj in objects.values():
        desc_type = DESC_TYPE_BY_KIND.get(obj.typ_komponentu or "")
        mac = obj.module_mac
        out_no = obj.leaf_out_no
        if desc_type is None or mac is None or out_no is None:
            continue
        if mac in colliding_macs:
            continue
        entry = entries_by_key.get(mac, {}).get((desc_type, out_no))
        if entry is None:
            continue
        out[obj.id] = DesignerResolution(
            location=location_names.get(entry.out_loc) if entry.out_loc else None,
            matter_device_type=entry.out_type or None,
        )
    return out
```

- [ ] **Step 4: Run the suite**

Run:
`uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/ampio_mqtt/_protocol.py tests/test_descriptions.py
git commit -m "Join objects to their description entries (#25)"
```

---

### Task 6: Store merge (apply_designer_metadata)

**Goal:** The store holds the resolved table, updates known objects (with
events), and re-applies it on every catalogue merge, so a refresh never wipes
`location`.

**Files:**

- Modify: `src/ampio_mqtt/_store.py`
- Test: `tests/test_store.py` (append)

**Acceptance Criteria:**

- [ ] `apply_designer_metadata` sets `location` and refines
      `matter_device_type`; a CAN value never clears a DB value
      (`matter_device_type=None` in a resolution leaves the column value
      standing).
- [ ] Only real changes emit `ObjectUpdated` - re-applying the same table emits
      nothing.
- [ ] After eviction and re-creation through a catalogue reply, the object
      regains its `location` from the held table.

**Verify:** `uv run pytest tests/test_store.py -q` -> all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store.py`;
      follow the file's existing helpers for building catalogue payloads and
      reading `Applied.events`)

```python
def test_apply_designer_metadata_sets_location_and_refines_type() -> None:
    store = AmpioStore()
    _seed_catalogue(store, {"id": 64, "typ_komponentu": "przekaznik",
                            "leafId": "0_cb89_257_2_0"})
    applied = store.apply_designer_metadata(
        {64: DesignerResolution(location="Potter", matter_device_type=256)}
    )
    assert store.objects[64].location == "Potter"
    assert store.objects[64].matter_device_type == 256
    assert [e.object.id for e in applied.events] == [64]
    # Re-applying the identical table is not news.
    assert store.apply_designer_metadata(
        {64: DesignerResolution(location="Potter", matter_device_type=256)}
    ).events == []


def test_designer_type_never_clears_the_db_column() -> None:
    store = AmpioStore()
    _seed_catalogue(store, {"id": 5, "typ_komponentu": "przekaznik",
                            "leafId": "0_cb89_257_2_1", "type": "266"})
    store.apply_designer_metadata(
        {5: DesignerResolution(location="Testowe", matter_device_type=None)}
    )
    assert store.objects[5].matter_device_type == 266
    assert store.objects[5].location == "Testowe"


def test_catalogue_merge_reapplies_the_designer_table() -> None:
    store = AmpioStore()
    row = {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"}
    _seed_catalogue(store, row)
    store.apply_designer_metadata(
        {64: DesignerResolution(location="Potter", matter_device_type=256)}
    )
    _seed_catalogue(store)      # eviction: empty catalogue
    _seed_catalogue(store, row)  # the object returns
    assert store.objects[64].location == "Potter"
    assert store.objects[64].matter_device_type == 256
```

Write `_seed_catalogue` as a small local helper that feeds a `devicesDetails`
`EndpointReply` through `store.apply` (mirror how the file's existing tests do
it), and import `DesignerResolution` from `ampio_mqtt._protocol`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_store.py -q` Expected: FAIL -
`apply_designer_metadata` does not exist.

- [ ] **Step 3: Implement** (`_store.py`)

In `__init__`, next to `_params_by_id`:

```python
        # `{object_id: DesignerResolution}` from the last resolve_locations()
        # sweep, kept so a catalogue refresh re-applies what the CAN record
        # proved (the catalogue itself never carries it).
        self._designer_by_id: dict[int, _protocol.DesignerResolution] = {}
```

Public method next to `begin_refresh`:

```python
    def apply_designer_metadata(
        self, resolved: dict[int, _protocol.DesignerResolution]
    ) -> Applied:
        """Hold the resolved designer table and fold it into known objects.

        ``location`` is authoritative from the record (None clears a stale
        name); ``matter_device_type`` refines and never clears - a record
        without a tag leaves the catalogue column's value standing.
        """
        applied = Applied()
        self._designer_by_id = dict(resolved)
        for oid, res in resolved.items():
            obj = self.objects.get(oid)
            if obj is None:
                continue
            updates: dict[str, Any] = {}
            if obj.location != res.location:
                updates["location"] = res.location
            if (
                res.matter_device_type is not None
                and obj.matter_device_type != res.matter_device_type
            ):
                updates["matter_device_type"] = res.matter_device_type
            if updates:
                obj = replace(obj, **updates)
                self.objects[oid] = obj
                self._record(obj, applied)
        return applied
```

In `_merge_metadata`, right after the `params` fallback block:

```python
        # The catalogue never carries the designer record's fields, so the
        # held table re-applies them on every merge - including the
        # re-creation after an eviction.
        designer = self._designer_by_id.get(meta.id)
        if designer is not None:
            updates["location"] = designer.location
            if designer.matter_device_type is not None:
                updates["matter_device_type"] = designer.matter_device_type
```

- [ ] **Step 4: Run the suite**

Run:
`uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/ampio_mqtt/_store.py tests/test_store.py
git commit -m "Hold and re-apply the resolved designer table in the store (#25, #110)"
```

---

### Task 7: Client resolve_locations (subscription, correlation, sweep)

**Goal:** `AmpioClient.resolve_locations()` sweeps every catalogued module over
get_data, joins the replies, merges them through the store, dispatches the
events, and returns `{object_id: location_name}`.

**Files:**

- Modify: `src/ampio_mqtt/client.py`
- Test: `tests/test_descriptions.py` (append)

**Acceptance Criteria:**

- [ ] The admin client subscribes `device_api/from/+/info`; the restricted
      client does not.
- [ ] `resolve_locations()` publishes one `device_api/to/<machex>/get_data` per
      catalogued module mac and returns the joined map; `AmpioObject.location`
      reads the value afterwards; an `ObjectUpdated` fired for the change.
- [ ] A module that never answers within the timeout is skipped - the call still
      returns what resolved, and raises nothing for the silence.
- [ ] On the restricted tier the call raises `RuntimeError` naming the tier.
- [ ] Coverage >= 95%, ruff clean.

**Verify:** `uv run pytest tests/test_descriptions.py -q` -> all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** (append to
      `tests/test_descriptions.py`)

```python
import asyncio

import pytest
from conftest import (
    ADMIN_DETAILS_TOPIC,
    ADMIN_DEVICES_TOPIC,
    ADMIN_USER,
    FakeBroker,
    deliver_later,
    details,
    devices,
    feed,
)

from ampio_mqtt import AmpioClient, ObjectUpdated

LOCATIONS_TOPIC = f"ampio/fromDB/{ADMIN_USER}/config/locations"


async def _admin_client_with_catalogue() -> tuple[AmpioClient, FakeBroker]:
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.01)
    feed(client, ADMIN_DETAILS_TOPIC, details(
        {"id": 64, "typ_komponentu": "przekaznik", "leafId": "0_cb89_257_2_0"}
    ))
    feed(client, ADMIN_DEVICES_TOPIC, devices({"id": 16, "mac": 0xCB89}))
    broker.published.clear()
    return client, broker


async def test_admin_subscribes_the_device_api_wildcard() -> None:
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        assert "device_api/from/+/info" in broker.subscribed
    finally:
        await client.stop()
    restricted_broker = FakeBroker()
    restricted = AmpioClient(
        "host", username="u", mqtt_client_factory=restricted_broker.factory
    )
    await restricted.start(timeout=2.0, discovery_timeout=0.01)
    try:
        assert "device_api/from/+/info" not in restricted_broker.subscribed
    finally:
        await restricted.stop()


async def test_resolve_locations_sweeps_joins_and_merges() -> None:
    client, broker = await _admin_client_with_catalogue()
    try:
        events: list[ObjectUpdated] = []
        client.subscribe(events.append, of=ObjectUpdated, object_id=64)
        info_payload = json.dumps(
            {"descriptions": base64.b64encode(frame(12, 0, 14, 256, "L")).decode()}
        )
        delivery = deliver_later(
            client,
            (LOCATIONS_TOPIC, json.dumps({"List": [{"id": 14, "opis_menu": "Potter"}]})),
            ("device_api/from/CB89/info", info_payload),
        )
        try:
            result = await client.resolve_locations(timeout=1.0)
        finally:
            await delivery
        assert result == {64: "Potter"}
        assert client.objects[64].location == "Potter"
        assert client.objects[64].matter_device_type == 256
        assert ("device_api/to/cb89/get_data", b"") in broker.published
        assert [e.object.location for e in events] == ["Potter"]
    finally:
        await client.stop()


async def test_resolve_locations_tolerates_silent_modules() -> None:
    client, _ = await _admin_client_with_catalogue()
    try:
        feed(client, ADMIN_DEVICES_TOPIC, devices(
            {"id": 16, "mac": 0xCB89}, {"id": 17, "mac": 0xBEEF}
        ))
        info_payload = json.dumps(
            {"descriptions": base64.b64encode(frame(12, 0, 14, 256, "L")).decode()}
        )
        delivery = deliver_later(
            client,
            (LOCATIONS_TOPIC, json.dumps({"List": [{"id": 14, "opis_menu": "Potter"}]})),
            ("device_api/from/CB89/info", info_payload),
        )
        try:
            result = await client.resolve_locations(timeout=0.2)
        finally:
            await delivery
        assert result == {64: "Potter"}
    finally:
        await client.stop()


async def test_resolve_locations_raises_on_restricted_tier() -> None:
    broker = FakeBroker()
    client = AmpioClient("host", username="u", mqtt_client_factory=broker.factory)
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        with pytest.raises(RuntimeError, match="admin"):
            await client.resolve_locations(timeout=0.1)
    finally:
        await client.stop()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_descriptions.py -q` Expected: FAIL - no
subscription, no method.

- [ ] **Step 3: Implement** (`client.py`)

In `_subscriptions`, extend the admin block:

```python
            topics += [
                *RAW_INPUT_WILDCARDS,
                RAW_DIAGNOSTICS_WILDCARD,
                RAW_EVENT_WILDCARD,
                _protocol.DEVICE_API_INFO_WILDCARD,
            ]
```

In `__init__`, next to `_poisoned_topics`:

```python
        # Per-mac futures awaiting a device_api info reply; every waiter
        # for a mac receives the same reply, exactly as endpoint fetches
        # share one.
        self._descriptions_waiters: dict[
            int, list[asyncio.Future[tuple[_protocol.OutputDescription, ...]]]
        ] = {}
```

In `_handle_message`, after the `EndpointReply` pure-parse branch and before
`store.apply`:

```python
            if isinstance(msg, _protocol.DeviceDescriptions):
                for future in self._descriptions_waiters.pop(msg.mac, []):
                    if not future.done():
                        future.set_result(msg.entries)
                return
```

The method, next to `fetch_locations`:

```python
    async def resolve_locations(self, timeout: float = 10.0) -> dict[int, str]:
        """Resolve every object's Designer location and return the map.

        Fetches the locations name table, asks each catalogued module for
        its CAN-resident description record over the ``device_api`` tree,
        joins the entries to objects, and folds the result into the store:
        :pyattr:`AmpioObject.location` carries the name afterwards, and a
        record's Matter tag refines :pyattr:`AmpioObject.matter_device_type`
        (#110). Changes dispatch as :class:`ObjectUpdated`.

        Returns ``{object_id: location_name}`` for what resolved. A module
        that does not answer within ``timeout`` is skipped without error -
        offline modules are normal - so the map can be partial; call again
        for another sweep. Admin tier only: the ``device_api`` tree answers
        no other account, and the call raises ``RuntimeError`` for one.
        Requires ``start()`` to have completed. Raises
        ``AmpioConnectionError`` if the broker is not connected and
        ``AmpioTimeoutError`` if the name table itself does not arrive.
        """
        if self._tier is not AccessTier.ADMIN:
            raise RuntimeError(
                "resolve_locations() needs the reserved admin login - the "
                "device_api tree answers no other account"
            )
        names = await self.fetch_locations(timeout=timeout)
        macs = sorted(
            {mod.mac for mod in self._store.modules.values() if mod.mac is not None}
        )
        loop = asyncio.get_running_loop()
        futures: dict[
            int, asyncio.Future[tuple[_protocol.OutputDescription, ...]]
        ] = {}
        for mac in macs:
            future = loop.create_future()
            futures[mac] = future
            self._descriptions_waiters.setdefault(mac, []).append(future)
        try:
            # The publishes sit inside the window, as _fetch's do; the
            # window elapsing is not an error - it bounds how long the
            # sweep waits for stragglers.
            async with asyncio.timeout(timeout):
                for mac in macs:
                    await self._connection.publish(
                        _protocol.device_api_request_topic(mac), b""
                    )
                await asyncio.gather(*futures.values())
        except TimeoutError:
            pass
        finally:
            for mac, future in futures.items():
                waiters = self._descriptions_waiters.get(mac)
                if waiters is not None:
                    if future in waiters:
                        waiters.remove(future)
                    if not waiters:
                        del self._descriptions_waiters[mac]
        by_mac = {
            mac: future.result()
            for mac, future in futures.items()
            if future.done() and not future.cancelled()
        }
        resolved = _protocol.resolve_designer(
            self._store.objects, by_mac, names, self._store.colliding_macs
        )
        applied = self._store.apply_designer_metadata(resolved)
        for event in applied.events:
            self._dispatch(event)
        return {
            oid: res.location
            for oid, res in resolved.items()
            if res.location is not None
        }
```

- [ ] **Step 4: Run the suite**

Run:
`uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/ampio_mqtt/client.py tests/test_descriptions.py
git commit -m "Sweep the device_api records and resolve object locations (#25)"
```

---

### Task 8: Harness claim and live verification

**Goal:** A credential-free harness claim proves the end-to-end path against the
fake M-SERV, and one live run against the real broker confirms the known values.

**USER-ORDERED GATE - NON-SKIPPABLE.** This task was requested by the user in
the current conversation. It MUST NOT be closed by walking around it, by
declaring it "verified inline", or by substituting a cheaper check. Close only
after every item in `acceptanceCriteria` has been re-validated independently,
with output captured.

**Files:**

- Create: `.verify-harness/claim18_designer_location.py`
- Modify: `.verify-harness/fixtures.py`

**Acceptance Criteria:**

- [ ] The fake M-SERV answers `locations` on the config surface and `get_data`
      on `device_api/to/#` (reply on `device_api/from/<MAC>/info` with an
      uppercase-hex mac), and the claim asserts: `resolve_locations()` returns
      the expected map, `objects[...].location` reads it, `matter_device_type`
      was refined, and the restricted client raises `RuntimeError`.
- [ ] The claim run output is captured in the task close
      (`uv run --with . python .verify-harness/claim18_designer_location.py`
      against the local mosquitto, per `.verify-harness` conventions).
- [ ] One live run against the real broker (admin env) captured:
      `resolve_locations()` output includes the relay object documented in the
      join-proof results with its known location name, and that object's
      `matter_device_type` reads 256 while its DB `type` column is empty (the
      #110 divergence, observed through the library).
- [ ] The live run wrote nothing: only `get_data`, discovery, and `locations`
      requests were published.

**Verify:**
`uv run --with . python .verify-harness/claim18_designer_location.py` -> prints
the expected admin map and the restricted `RuntimeError`; then the live one-off
run's captured output.

**Steps:**

- [ ] **Step 1: Extend `fixtures.py`.** Give `OB5` a `leafId` of
      `0_cb89_257_2_0` and `typ_komponentu` `przekaznik` if it lacks them; make
      sure that the admin `devices` reply carries a module row with `mac` 52105
      (0xCB89). Add to `response_table` (admin user only):
      `(f"ampio/control/{user}/config", "locations")` ->
      `(f"ampio/fromDB/{user}/config/locations", rows({"id": 14, "opis_menu": "Potter"}))`.
      In `fake_mserv`, also subscribe `device_api/to/#` and answer any
      `device_api/to/<machex>/get_data` with a JSON
      `{"descriptions": <base64 frame (12, 0, 14, 256, "L")>}` on
      `device_api/from/<MACHEX>/info` (uppercase hex). Build the frame with the
      same byte layout as `tests/test_descriptions.py`'s `frame()` helper.

- [ ] **Step 2: Write `claim18_designer_location.py`** following
      `claim17_matter_device_type.py`'s shape:

```python
"""Contract (#25): resolve_locations() populates AmpioObject.location.

Expected:
  admin: resolve -> {5: 'Potter'}; objects[5].location='Potter';
         objects[5].matter_device_type=256 (refined from the CAN record)
  restricted: resolve_locations() raises RuntimeError
"""

from __future__ import annotations

import asyncio

from ampio_mqtt import AmpioClient

from fixtures import ADMIN, PORT, USER, fake_mserv


async def main() -> None:
    ready = asyncio.Event()
    server = asyncio.create_task(fake_mserv(user=ADMIN, ready=ready))
    await asyncio.wait_for(ready.wait(), 5)
    client = AmpioClient("127.0.0.1", ADMIN, port=PORT)
    assert await client.start(timeout=5.0, discovery_timeout=5.0)
    try:
        resolved = await client.resolve_locations(timeout=5.0)
        print(f"admin: resolved={resolved}")
        print(f"admin: location={client.objects[5].location}")
        print(f"admin: matter_device_type={client.objects[5].matter_device_type}")
    finally:
        await client.stop()
        server.cancel()

    ready = asyncio.Event()
    server = asyncio.create_task(fake_mserv(user=USER, ready=ready))
    await asyncio.wait_for(ready.wait(), 5)
    restricted = AmpioClient("127.0.0.1", USER, port=PORT)
    assert await restricted.start(timeout=5.0, discovery_timeout=5.0)
    try:
        try:
            await restricted.resolve_locations(timeout=1.0)
            print("restricted: NO ERROR - CLAIM FAILED")
        except RuntimeError as err:
            print(f"restricted: RuntimeError={err}")
    finally:
        await restricted.stop()
        server.cancel()


if __name__ == "__main__":
    asyncio.run(main())
```

Adapt the object id and fixture names to what `fixtures.py` actually defines -
the claim must run green against the extended fake, with mosquitto started per
the `.verify-harness` recipe.

- [ ] **Step 3: Run the claim and capture the output.**

- [ ] **Step 4: Live verification** (admin env; read-only). Run a one-off script
      that starts `AmpioClient` against the real broker, awaits discovery, calls
      `resolve_locations(timeout=15.0)`, and prints: the size of the returned
      map, the entry for the join-proof relay object, and that object's
      `matter_device_type`. Capture the output. Restore nothing - nothing was
      written.

- [ ] **Step 5: Commit**

```bash
git add .verify-harness/claim18_designer_location.py .verify-harness/fixtures.py
git commit -m "Harness claim for resolve_locations (#25)"
```

---

### Task 9: Docs, CHANGELOG, version 0.30.0

**Goal:** The docs carry the new wire contract, the CHANGELOG describes 0.30.0,
and the version is bumped.

**Files:**

- Modify: `docs/identity.md`, `docs/protocol.md`, `docs/discovery-flow.md`,
  `docs/untapped-surfaces.md`, `CHANGELOG.md`, `src/ampio_mqtt/__init__.py`

**Acceptance Criteria:**

- [ ] `docs/identity.md` gains "The Designer location (per-output `outLoc`)":
      the get_data pair, the frame layout, the descType enum table, the join
      rule (`leaf_out_no` + `DESC_TYPE_BY_KIND`), the dead DB `lokalizacja`
      column, and the tier gate. The Matter-tag section notes that the CAN
      record is authoritative and the DB mirror lags (#110), and that
      `resolve_locations()` refines the field.
- [ ] `docs/untapped-surfaces.md`: the #25 row now reads as shipped (or is
      removed); the table stays consistent.
- [ ] `CHANGELOG.md` gains a `## 0.30.0` section: Added (`AmpioObject.location`,
      `resolve_locations()`, restored `fetch_locations()`, `leaf_out_no`),
      Changed (`matter_device_type` refined from the CAN record; `_fetch` tier
      guard), with issue references #25 and #110.
- [ ] `__version__ = "0.30.0"`.
- [ ] `uv run pytest -q`, `ruff check`, `ruff format --check` all green.

**Verify:**
`uv run pytest -q && uv run ruff check src tests tools && uv run ruff format --check src tests tools`
-> green; `grep '__version__' src/ampio_mqtt/__init__.py` -> `0.30.0`.

**Steps:**

- [ ] **Step 1: Write the identity.md section** (place it after the Matter-tag
      section; carry the wire contract from this plan's Global Constraints
      verbatim where applicable, minus the install-specific numbers).
- [ ] **Step 2: Update protocol.md** (document the `device_api` get_data pair
      and the `locations` endpoint row next to the other request/reply surfaces)
      **and discovery-flow.md** (name `resolve_locations()` in the on-demand
      list).
- [ ] **Step 3: Update untapped-surfaces.md** (drop or rewrite the #25 row).
- [ ] **Step 4: CHANGELOG + version bump.**
- [ ] **Step 5: Run the full suite and commit**

```bash
git add docs CHANGELOG.md src/ampio_mqtt/__init__.py
git commit -m "0.30.0: resolve per-output Designer locations (#25, #110)"
```

---

## Self-review notes

- Type names are consistent across tasks: `OutputDescription`,
  `DeviceDescriptions`, `DesignerResolution`, `DESC_TYPE_BY_KIND`,
  `device_api_request_topic`, `leaf_out_no`, `apply_designer_metadata`,
  `resolve_locations`, `fetch_locations`.
- Task 3 depends on Task 1 (constants); Tasks 5-7 depend on 3 and 4; Task 6
  precedes 7; Task 8 needs 7; Task 9 closes.
- The release itself (tag, PyPI approval, GitHub Release) is out of scope here -
  the release-process conventions handle it after review.
