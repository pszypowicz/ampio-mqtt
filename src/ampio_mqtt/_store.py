"""Everything the library knows, and how an inbound message changes it.

Pure state: no sockets, no tasks, no listeners. `apply()` takes one MQTT
message and reports what it touched, so the caller decides who to tell. That
also makes every protocol behaviour here reachable from a plain function call.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from . import _protocol
from .const import (
    ENDPOINTS,
    Endpoint,
    classify,
    input_channel_prefix,
    response_topic,
)
from .models import (
    AmpioEvent,
    AmpioModule,
    AmpioObject,
    AmpioServerInfo,
    AmpioState,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Applied:
    """What one inbound message did to the store."""

    # The endpoint whose reply this was, if any, and whether its payload could
    # be read - together they tell the caller when discovery has advanced.
    endpoint: Endpoint | None = None
    parsed: bool = True
    objects: list[AmpioObject] = field(default_factory=list)
    modules: list[AmpioModule] = field(default_factory=list)
    events: list[AmpioEvent] = field(default_factory=list)


class AmpioStore:
    """Applies M-SERV messages to the object, module and server state."""

    def __init__(self, user: str) -> None:
        self.state = AmpioState()
        self._by_response: dict[str, Endpoint] = {
            response_topic(ep, user): ep for ep in ENDPOINTS
        }
        # Raw-channel bridge: (module mac, prefix, channel) -> object id.
        self._input_index: dict[tuple[int, str, int], int] = {}
        # Effective bus mac -> module, for routing a module's own broadcasts.
        self._module_by_mac: dict[int, AmpioModule] = {}
        # Macs the devices catalogue reports on more than one module. Nothing
        # on the wire enforces uniqueness, so this is the signal a consumer
        # keying devices on mac needs to avoid silently merging two modules.
        self._colliding_macs: frozenset[int] = frozenset()
        # Objects a raw-channel message has actually arrived for. The raw form
        # leads the per-object echo, so once an input is raw-proven the slower
        # echo is dropped rather than re-notifying with a stale value.
        self._raw_seen_ids: set[int] = set()
        # Full-catalogue `{object_id: params}` from `data/params_devices`, kept
        # because the app-sync catalogue carries no params column and the two
        # replies arrive in no fixed order.
        self._params_by_id: dict[int, int] = {}
        # Endpoints whose reply mutates state, each reporting whether the
        # payload parsed. The rest are pure request/response - the client keeps
        # their payload and parses it on demand.
        self._handlers: dict[str, Callable[[str], bool]] = {
            "details": partial(self._handle_catalogue, "devicesDetails"),
            "data_devices": partial(self._handle_catalogue, "data/devices"),
            "devices": self._handle_devices,
            "params_devices": self._handle_params_devices,
            "states": self._handle_states_snapshot,
            "info": self._handle_info,
        }
        self._applied = Applied()

    # --- routing ----------------------------------------------------------

    def apply(self, topic: str, payload: str) -> Applied:
        """Apply one message and report what it changed."""
        self._applied = Applied()
        endpoint = self._by_response.get(topic)
        if endpoint is not None:
            self._applied.endpoint = endpoint
            handler = self._handlers.get(endpoint.name)
            self._applied.parsed = handler is None or handler(payload)
        elif topic.endswith("/state") and "/ob/" in topic:
            self._handle_state(topic, payload)
        elif topic.startswith("ampio/from/") and "/state/" in topic:
            self._handle_raw_channel(topic, payload)
        elif topic.startswith("ampio/from/") and "/b/" in topic:
            self._handle_diagnostics(topic, payload)
        elif topic.startswith("ampio/from/") and topic.endswith("/event"):
            self._handle_event(topic, payload)
        return self._applied

    # --- catalogues -------------------------------------------------------

    def _handle_catalogue(self, surface: str, payload: str) -> bool:
        """Apply an object catalogue from either discovery surface.

        The two surfaces carry the same rows: the app-sync one simply omits
        ``params`` (which the ``params_devices`` table supplies instead) and
        ``stan_json``, so one merge covers both. On the admin tier both answer,
        and the second pass changes nothing.
        """
        items = _protocol.parse_details(payload)
        if items is None:
            _LOGGER.warning("Could not parse Ampio %s catalogue", surface)
            return False
        touched = False
        for meta in items:
            touched |= self._merge_metadata(meta)
        if touched:
            self._rebuild_indexes()
        return True

    def _merge_metadata(self, meta: _protocol.ObjectMetadata) -> bool:
        """Fold one catalogue row into its object; True when anything changed.

        Only a real change is reported, so re-requesting the catalogue on every
        reconnect does not hand a consumer a full set of updates that say
        nothing new.
        """
        obj = self.state.objects.get(meta.id)
        if obj is None:
            obj = AmpioObject(id=meta.id)
            self.state.objects[meta.id] = obj
        before = _identity(obj)
        obj.device_id = meta.device_id
        obj.typ_komponentu = meta.typ_komponentu
        obj.name = meta.name or obj.name
        obj.interpretacja = meta.interpretacja
        obj.funkcja = meta.funkcja
        obj.leaf_id = meta.leaf_id
        # A row without the column leaves the params_devices value standing.
        params = (
            meta.params if meta.params is not None else self._params_by_id.get(meta.id)
        )
        if params is not None:
            obj.params = params
        obj.kind = classify(meta.typ_komponentu, meta.interpretacja)
        changed = before != _identity(obj)
        if meta.stan_json is not None:
            changed |= self._apply_stan_json(obj, meta.stan_json)
        if changed:
            self._record(obj)
        return changed

    def _handle_devices(self, payload: str) -> bool:
        modules = _protocol.parse_devices(payload)
        if modules is None:
            _LOGGER.warning("Could not parse Ampio devices list")
            return False
        for module in modules:
            previous = self.state.modules.get(module.id)
            if previous is not None:
                module.last_seen = previous.last_seen
                module.supply_voltage = previous.supply_voltage
                module.temperature = previous.temperature
            self.state.modules[module.id] = module
        self._rebuild_indexes()
        return True

    def _handle_params_devices(self, payload: str) -> bool:
        """Apply the ``data/params_devices`` params table.

        Stores the full table for catalogue rows that arrive later, and updates
        objects already known. Ids with no known object create no placeholder:
        the table is not grant-filtered, so on a restricted account most of it
        refers to objects the account cannot otherwise see.
        """
        table = _protocol.parse_params_devices(payload)
        if table is None:
            _LOGGER.warning("Could not parse Ampio params_devices table")
            return False
        self._params_by_id = table
        for oid, params in table.items():
            obj = self.state.objects.get(oid)
            if obj is not None and obj.params != params:
                obj.params = params
                self._record(obj)
        return True

    def _handle_info(self, payload: str) -> bool:
        self.state.server_info = _protocol.parse_server_info(payload)
        return True

    def _handle_states_snapshot(self, payload: str) -> bool:
        entries = _protocol.parse_states_snapshot(payload)
        if entries is None:
            _LOGGER.warning("Could not parse Ampio states snapshot")
            return False
        for entry in entries:
            obj = self.state.objects.get(entry.id)
            if obj is None:
                obj = AmpioObject(id=entry.id, kind=classify(None, None))
                self.state.objects[entry.id] = obj
            if entry.stan_json is not None and self._apply_stan_json(
                obj, entry.stan_json
            ):
                self._record(obj)
        return True

    # --- live state -------------------------------------------------------

    def _handle_state(self, topic: str, payload: str) -> None:
        update = _protocol.parse_state_message(topic, payload)
        if update is None:
            return
        if update.id in self._raw_seen_ids:
            # The faster raw-channel path is authoritative for this input; drop
            # the slower per-object echo to avoid a double notify and a stale
            # echo clobbering a fresh raw edge.
            return
        obj = self.state.objects.get(update.id)
        if obj is None:
            # State raced ahead of the catalogues -> generic sensor until
            # metadata lands.
            obj = AmpioObject(id=update.id, kind=classify(None, None))
            self.state.objects[update.id] = obj
        obj.value = update.value
        if update.tilt is not None:
            obj.tilt_position = update.tilt
        obj.updated_at = float(update.on_ms) / 1000.0 if update.on_ms else time.time()
        self._touch_module(obj.device_id, update.on_ms)
        self._record(obj)

    def _handle_raw_channel(self, topic: str, payload: str) -> None:
        key = _protocol.parse_raw_channel_topic(topic)
        if key is None:
            return
        oid = self._input_index.get(key)
        if oid is None:
            return  # channel has no exposed Designer object - ignore
        obj = self.state.objects[oid]
        self._raw_seen_ids.add(oid)
        obj.value = payload.strip()
        obj.updated_at = time.time()
        self._touch_module(obj.device_id, None)
        self._record(obj)

    def _handle_diagnostics(self, topic: str, payload: str) -> None:
        mac = _protocol.parse_diagnostics_mac(topic)
        if mac is None:
            return
        module = self._module_by_mac.get(mac)
        if module is None:
            return  # a module the catalogue does not list
        diagnostics = _protocol.parse_diagnostics(payload)
        if diagnostics is None:
            return
        module.supply_voltage = diagnostics.supply_voltage
        module.temperature = diagnostics.temperature
        module.last_seen = time.time()
        self._applied.modules.append(module)

    def _handle_event(self, topic: str, payload: str) -> None:
        event = _protocol.parse_event(topic, payload)
        if event is not None:
            self._applied.events.append(event)

    # --- helpers ----------------------------------------------------------

    def _apply_stan_json(self, obj: AmpioObject, stan_json: str) -> bool:
        """Apply a bulk-snapshot value to `obj` when it is not older than what it holds.

        The per-object topics are not retained, so this snapshot is the only
        resync after a reconnect - during which the object may well have
        changed. It must therefore be able to correct a stale value, while
        still losing to the live push that can arrive first on a fresh
        connection.
        """
        seed = _protocol.parse_stan_json(stan_json)
        if seed is None:
            return False
        self._touch_module(obj.device_id, seed.on_ms)
        reported_at = None if seed.on_ms is None else float(seed.on_ms) / 1000.0
        if seed.value is None or not self._supersedes(obj, reported_at):
            return False
        changed = obj.value != seed.value or (
            seed.tilt is not None and obj.tilt_position != seed.tilt
        )
        obj.value = seed.value
        if seed.tilt is not None:
            obj.tilt_position = seed.tilt
        obj.updated_at = reported_at
        return changed

    @staticmethod
    def _supersedes(obj: AmpioObject, reported_at: float | None) -> bool:
        """Whether a snapshot report should replace what `obj` already holds.

        Undated reports only fill a gap; a dated one wins from the same instant
        onwards. Both sides are wall-clock epochs - the M-SERV's `on` field and,
        for the undated raw channel, local receive time.
        """
        if obj.value is None:
            return True
        if reported_at is None or obj.updated_at is None:
            return False
        return reported_at >= obj.updated_at

    def _touch_module(self, module_id: int | None, on_ms: float | None) -> None:
        """Mark the module as having reported now (or at `on_ms`, server time)."""
        if module_id is None:
            return
        module = self.state.modules.get(module_id)
        if module is None:
            return
        ts = float(on_ms) / 1000.0 if on_ms is not None and on_ms > 0 else time.time()
        if module.last_seen is None or ts > module.last_seen:
            module.last_seen = ts

    def _rebuild_indexes(self) -> None:
        """Rebuild the routing tables for the raw tree.

        Both are keyed on the module's effective bus address (`mac`, the
        Designer override) - never `mac_global`, which diverges from the
        raw-topic MAC on replaced modules. `(mac, prefix, channel)` routes an
        input channel to its object, covering only bridgeable input types with
        a known channel and module mac; `mac` alone routes a module's own
        diagnostics broadcast.

        A mac the catalogue reports on more than one module routes nothing:
        the sender of a raw-tree message on it is unknowable, and attributing
        it to an arbitrary module would silently corrupt that module's state.
        Affected inputs still update through the per-object state path.
        """
        self._refresh_colliding_macs()
        index: dict[tuple[int, str, int], int] = {}
        for obj in self.state.objects.values():
            prefix = input_channel_prefix(obj.typ_komponentu)
            if prefix is None or obj.funkcja is None or obj.device_id is None:
                continue
            module = self.state.modules.get(obj.device_id)
            if module is None or module.mac is None:
                continue
            if module.mac in self._colliding_macs:
                continue
            index[(module.mac, prefix, obj.funkcja)] = obj.id
        self._input_index = index
        self._module_by_mac = {
            module.mac: module
            for module in self.state.modules.values()
            if module.mac is not None and module.mac not in self._colliding_macs
        }
        # An object the index no longer covers must go back to its per-object
        # updates, or a mac change in Designer would freeze it for good.
        self._raw_seen_ids &= set(index.values())

    def _refresh_colliding_macs(self) -> None:
        """Recompute the colliding-mac set, warning when it changes.

        Warns only on a change, not on every catalogue parse - the catalogue
        is re-requested on every reconnect, and repeating a standing collision
        each time would drown the log in old news. A collision that resolves
        clears the set silently.
        """
        counts = Counter(
            module.mac
            for module in self.state.modules.values()
            if module.mac is not None
        )
        colliding = frozenset(mac for mac, n in counts.items() if n > 1)
        if colliding and colliding != self._colliding_macs:
            details = "; ".join(
                f"mac {mac} shared by "
                + ", ".join(
                    f"module {module.id} ({module.name or 'unnamed'})"
                    for module in self.state.modules.values()
                    if module.mac == mac
                )
                for mac in sorted(colliding)
            )
            _LOGGER.warning(
                "Ampio devices catalogue reports colliding module macs; "
                "raw-channel and diagnostics routing for them is disabled: %s",
                details,
            )
        self._colliding_macs = colliding

    def _record(self, obj: AmpioObject) -> None:
        self._applied.objects.append(obj)

    # --- read surface -----------------------------------------------------

    @property
    def objects(self) -> dict[int, AmpioObject]:
        return self.state.objects

    @property
    def modules(self) -> dict[int, AmpioModule]:
        return self.state.modules

    @property
    def colliding_macs(self) -> frozenset[int]:
        return self._colliding_macs

    @property
    def server_info(self) -> AmpioServerInfo | None:
        return self.state.server_info


def _identity(obj: AmpioObject) -> tuple[object, ...]:
    """The metadata fields a catalogue row can change."""
    return (
        obj.device_id,
        obj.typ_komponentu,
        obj.name,
        obj.interpretacja,
        obj.funkcja,
        obj.leaf_id,
        obj.params,
        obj.kind,
    )
