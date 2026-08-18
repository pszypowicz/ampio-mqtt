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
from .classification import classify, input_channel_prefix
from .endpoints import (
    BASELINE_SERVER_VERSION,
    AccessTier,
    Endpoint,
)
from .events import (
    BusEvent,
    ModuleRemoved,
    ModuleUpdated,
    ObjectRemoved,
    ObjectUpdated,
    StoreEvent,
)
from .models import (
    AmpioModule,
    AmpioObject,
    AmpioServerInfo,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Applied:
    """What one inbound message did to the store."""

    # The endpoint whose reply this was, if any, and whether its payload could
    # be read - together they tell the caller when discovery has advanced.
    endpoint: Endpoint | None = None
    parsed: bool = True
    # Everything the message changed, in processing order, ready to dispatch.
    # Removal events carry final state already gone from the store.
    events: list[StoreEvent] = field(default_factory=list)


class AmpioStore:
    """Applies M-SERV messages to the object, module and server state."""

    def __init__(self, user: str) -> None:
        self.objects: dict[int, AmpioObject] = {}
        self.modules: dict[int, AmpioModule] = {}
        self.server_info: AmpioServerInfo | None = None
        # The single home of topic-shape knowledge; the store only ever sees
        # typed inbound messages.
        self._router = _protocol.Router(user)
        # Raw-channel bridge: (module mac, prefix, channel) -> object id.
        self._input_index: dict[tuple[int, str, int], int] = {}
        # Effective bus mac -> module, for routing a module's own broadcasts.
        self._module_by_mac: dict[int, AmpioModule] = {}
        # Macs the devices catalogue reports on more than one module. Nothing
        # on the wire enforces uniqueness, so this is the signal a consumer
        # keying devices on mac needs to avoid silently merging two modules.
        self._colliding_macs: frozenset[int] = frozenset()
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
        match self._router.route(topic, payload):
            case _protocol.EndpointReply(endpoint=endpoint, payload=body):
                self._applied.endpoint = endpoint
                handler = self._handlers.get(endpoint.name)
                if handler is not None:
                    self._applied.parsed = handler(body)
                else:
                    # Pure request/response endpoints mutate nothing here, but
                    # their parseability still gates the reply signal - a
                    # corrupt reply must not complete a fetch. Every one of
                    # them answers with a {"List": [...]} document, so that
                    # shape check stands in for a handler; an endpoint with a
                    # different reply shape needs its own _handlers entry.
                    self._applied.parsed = _protocol._rows(body) is not None
                    if not self._applied.parsed:
                        _LOGGER.warning("Could not parse Ampio %s reply", endpoint.name)
            case _protocol.StateUpdate() as update:
                self._apply_state(update)
            case _protocol.RawChannelEdge() as edge:
                self._apply_raw_channel(edge)
            case _protocol.DiagnosticsReport(mac=mac, diagnostics=diagnostics):
                self._apply_diagnostics(mac, diagnostics)
            case BusEvent() as event:
                self._applied.events.append(event)
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
        evicted = self._evict_missing_objects(surface, {meta.id for meta in items})
        if touched or evicted:
            self._rebuild_indexes()
        return True

    def _evict_missing_objects(self, surface: str, present: set[int]) -> bool:
        """Drop objects the authoritative catalogue no longer lists.

        A reply proves absence only when it is complete for the account. The
        ``config`` catalogue always is - the M-SERV serves it to
        administrators only, so its arrival is itself the proof. The
        app-sync catalogue is complete only for a RESTRICTED account (its
        grant bounds everything the store could ever hold); on the admin
        tier it is a second, differently-scoped view and must not evict. An
        empty reply against a populated store is refused outright: no
        observed server produces one, and honoring it would tell a consumer
        to drop every entity it has.
        """
        if surface == "data/devices":
            info = self.server_info
            if info is None or info.access_tier is not AccessTier.RESTRICTED:
                return False
        missing = [oid for oid in self.objects if oid not in present]
        if not missing:
            return False
        if not present:
            _LOGGER.warning(
                "Ampio %s catalogue reply is empty while %d objects are known; "
                "refusing to evict them",
                surface,
                len(missing),
            )
            return False
        for oid in missing:
            obj = self.objects.pop(oid)
            self._params_by_id.pop(oid, None)
            self._applied.events.append(ObjectRemoved(obj))
        return True

    def _merge_metadata(self, meta: _protocol.ObjectMetadata) -> bool:
        """Fold one catalogue row into its object; True when anything changed.

        Only a real change is reported, so re-requesting the catalogue on every
        reconnect does not hand a consumer a full set of updates that say
        nothing new.
        """
        obj = self.objects.get(meta.id)
        if obj is None:
            obj = AmpioObject(id=meta.id)
            self.objects[meta.id] = obj
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
            previous = self.modules.get(module.id)
            if previous is not None:
                module.last_seen = previous.last_seen
                module.supply_voltage = previous.supply_voltage
                module.temperature = previous.temperature
            self.modules[module.id] = module
        # The module list is admin-only and complete, so its arrival is the
        # authority to evict what it stopped listing - with the same
        # empty-reply guard the object catalogues apply.
        present = {module.id for module in modules}
        missing = [mid for mid in self.modules if mid not in present]
        if missing and not present:
            _LOGGER.warning(
                "Ampio devices reply is empty while %d modules are known; "
                "refusing to evict them",
                len(missing),
            )
        else:
            for mid in missing:
                self._applied.events.append(ModuleRemoved(self.modules.pop(mid)))
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
            obj = self.objects.get(oid)
            if obj is not None and obj.params != params:
                obj.params = params
                self._record(obj)
        return True

    def _handle_info(self, payload: str) -> bool:
        previous = self.server_info
        info = _protocol.parse_server_info(payload)
        self.server_info = info
        # Warn when the version first becomes known or changes, not on the
        # re-request every reconnect issues.
        if (
            previous is None or previous.server_version != info.server_version
        ) and _protocol.server_below_baseline(info.server_version):
            _LOGGER.warning(
                "Ampio server reports version %s, below the tested baseline %s; "
                "behavior on this server is untested - upgrade the M-SERV",
                info.server_version or "(none)",
                ".".join(map(str, BASELINE_SERVER_VERSION)),
            )
        return True

    def _handle_states_snapshot(self, payload: str) -> bool:
        entries = _protocol.parse_states_snapshot(payload)
        if entries is None:
            _LOGGER.warning("Could not parse Ampio states snapshot")
            return False
        for entry in entries:
            obj = self.objects.get(entry.id)
            if obj is None:
                obj = AmpioObject(id=entry.id, kind=classify(None, None))
                self.objects[entry.id] = obj
            if entry.stan_json is not None and self._apply_stan_json(
                obj, entry.stan_json
            ):
                self._record(obj)
        return True

    # --- live state -------------------------------------------------------

    def _apply_state(self, update: _protocol.StateUpdate) -> None:
        obj = self.objects.get(update.id)
        if obj is not None and obj.raw_proven:
            # The faster raw-channel path is authoritative for this input, so
            # the echo's value must neither re-notify nor clobber a newer
            # edge. Its server timestamp is still harvested: the raw tree is
            # undated, so the echo is what anchors a raw-proven object to the
            # server clock and makes snapshot supersession comparable.
            if update.on_ms:
                reported_at = float(update.on_ms) / 1000.0
                if (
                    obj.updated_at_clock == "local"
                    or obj.updated_at is None
                    or reported_at >= obj.updated_at
                ):
                    obj.updated_at = reported_at
                    obj.updated_at_clock = "server"
                self._touch_module(obj.device_id)
            return
        if obj is None:
            # State raced ahead of the catalogues -> generic sensor until
            # metadata lands.
            obj = AmpioObject(id=update.id, kind=classify(None, None))
            self.objects[update.id] = obj
        obj.value = update.value
        if update.tilt is not None:
            obj.tilt_position = update.tilt
        if update.on_ms:
            obj.updated_at = float(update.on_ms) / 1000.0
            obj.updated_at_clock = "server"
        else:
            obj.updated_at = time.time()
            obj.updated_at_clock = "local"
        self._touch_module(obj.device_id)
        self._record(obj)

    def _apply_raw_channel(self, edge: _protocol.RawChannelEdge) -> None:
        oid = self._input_index.get((edge.mac, edge.prefix, edge.channel))
        if oid is None:
            return  # channel has no exposed Designer object - ignore
        obj = self.objects[oid]
        obj.raw_proven = True
        obj.value = edge.value
        obj.updated_at = time.time()
        obj.updated_at_clock = "local"
        self._touch_module(obj.device_id)
        self._record(obj)

    def _apply_diagnostics(
        self, mac: int, diagnostics: _protocol.ModuleDiagnostics
    ) -> None:
        module = self._module_by_mac.get(mac)
        if module is None:
            return  # a module the catalogue does not list
        module.supply_voltage = diagnostics.supply_voltage
        module.temperature = diagnostics.temperature
        module.last_seen = time.time()
        self._applied.events.append(ModuleUpdated(module))

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
        obj.updated_at_clock = None if reported_at is None else "server"
        return changed

    def _supersedes(self, obj: AmpioObject, reported_at: float | None) -> bool:
        """Whether a server-dated snapshot report should replace what `obj` holds.

        Undated reports only fill a gap. A dated report beats an undated
        value, and beats a server-dated one from the same instant onwards -
        both sides are then the M-SERV's own clock, so RTC skew cancels out.
        A locally-dated value (a raw edge whose anchoring echo has not
        arrived yet) is never compared against the server clock: the two can
        disagree by an arbitrary RTC error, so the report is rejected and
        the echo, due within ~150 ms, re-anchors the object for the next
        comparison.
        """
        if obj.value is None:
            return True
        if reported_at is None:
            return False
        if obj.updated_at is None:
            return True
        if obj.updated_at_clock == "local":
            return False
        return reported_at >= obj.updated_at

    def _touch_module(self, module_id: int | None) -> None:
        """Mark the module as having produced live evidence just now.

        One clock only: the local receive time, because a live message is by
        definition received "now". Snapshot and catalogue seeds do not touch
        this - they replay DB state that may be arbitrarily old, which says
        nothing about whether the module is alive.
        """
        if module_id is None:
            return
        module = self.modules.get(module_id)
        if module is not None:
            module.last_seen = time.time()

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
        for obj in self.objects.values():
            prefix = input_channel_prefix(obj.typ_komponentu)
            if prefix is None or obj.funkcja is None or obj.device_id is None:
                continue
            module = self.modules.get(obj.device_id)
            if module is None or module.mac is None:
                continue
            if module.mac in self._colliding_macs:
                continue
            index[(module.mac, prefix, obj.funkcja)] = obj.id
        self._input_index = index
        self._module_by_mac = {
            module.mac: module
            for module in self.modules.values()
            if module.mac is not None and module.mac not in self._colliding_macs
        }
        # An object the index no longer covers must go back to its per-object
        # updates, or a mac change in Designer would freeze it for good.
        covered = set(index.values())
        for oid, obj in self.objects.items():
            if obj.raw_proven and oid not in covered:
                obj.raw_proven = False

    def _refresh_colliding_macs(self) -> None:
        """Recompute the colliding-mac set, warning when it changes.

        Warns only on a change, not on every catalogue parse - the catalogue
        is re-requested on every reconnect, and repeating a standing collision
        each time would drown the log in old news. A collision that resolves
        clears the set silently.
        """
        counts = Counter(
            module.mac for module in self.modules.values() if module.mac is not None
        )
        colliding = frozenset(mac for mac, n in counts.items() if n > 1)
        if colliding and colliding != self._colliding_macs:
            details = "; ".join(
                f"mac {mac} shared by "
                + ", ".join(
                    f"module {module.id} ({module.name or 'unnamed'})"
                    for module in self.modules.values()
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
        self._applied.events.append(ObjectUpdated(obj))

    # --- read surface -----------------------------------------------------

    @property
    def colliding_macs(self) -> frozenset[int]:
        return self._colliding_macs


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
