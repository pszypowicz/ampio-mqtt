"""Everything the library knows, and how an inbound message changes it.

Pure state: no sockets, no tasks, no listeners. `apply()` takes one MQTT
message and reports what it touched, so the caller decides who to tell. That
also makes every protocol behaviour here reachable from a plain function call.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from functools import partial
from typing import Any

from . import _protocol
from .classification import input_channel_prefix
from .events import (
    BusEvent,
    ModuleRemoved,
    ModuleUpdated,
    ObjectAdded,
    ObjectRemoved,
    ObjectUpdated,
    StoreEvent,
)
from .models import (
    AmpioModule,
    AmpioObject,
    AmpioServerInfo,
    DesignerRecord,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Applied:
    """What one inbound message did to the store."""

    # Whether the payload could be read - an endpoint reply that could not
    # must not advance discovery or resolve a fetch.
    parsed: bool = True
    # Everything the message changed, in processing order, ready to dispatch.
    # Update events carry a snapshot taken as the change was applied, so a
    # consumer that defers processing still sees the state the event was
    # about; removal events carry final state already gone from the store.
    events: list[StoreEvent] = field(default_factory=list)


class AmpioStore:
    """Applies typed M-SERV messages to the object, module and server state.

    Tier-agnostic by design: which surfaces can deliver is decided upstream
    (the client subscribes and routes only the account tier's endpoints),
    so every message that reaches ``apply`` is one the account is served.
    """

    def __init__(self) -> None:
        self.objects: dict[int, AmpioObject] = {}
        self.modules: dict[int, AmpioModule] = {}
        self.server_info: AmpioServerInfo | None = None
        # Override macs shared by two or more catalogue rows. The raw
        # routing tables are keyed by mac, so edges and diagnostics on a
        # colliding mac cannot be attributed reliably; the collision is
        # warned once per change and surfaced for diagnostics.
        self.colliding_macs: frozenset[int] = frozenset()
        # Raw-channel bridge: (module mac, prefix, channel) -> object id.
        self._input_index: dict[tuple[int, str, int], int] = {}
        # Effective bus mac -> module id, for routing a module's own
        # broadcasts. Ids, not instances: modules are frozen and replaced on
        # every change, so a cached instance would go stale.
        self._module_id_by_mac: dict[int, int] = {}
        # Full-catalogue `{object_id: params}` from `data/params_devices`, kept
        # because the app-sync catalogue carries no params column and the two
        # replies arrive in no fixed order.
        self._params_by_id: dict[int, int] = {}
        # `{object_id: DesignerRecord}` from the last resolve_locations()
        # sweep, kept so a catalogue refresh re-applies what the CAN record
        # proved (the catalogue itself never carries it).
        self._designer_by_id: dict[int, DesignerRecord] = {}
        # `{mac: module-level location}` accumulated across sweeps, kept for
        # the same reason on the module side; None is an authoritative
        # "answered, unassigned".
        self._module_location_by_mac: dict[int, str | None] = {}
        # `{object_id: stan_json}` from the last `data/states` snapshot,
        # kept for the same reason; a snapshot row for an id no catalogue
        # established creates nothing.
        self._stan_by_id: dict[int, str] = {}
        # Latest live push per id no catalogue has established, with its
        # local receive time. Only the catalogues decide which objects
        # exist, so a push that races ahead of them waits here and
        # surfaces with the catalogue row.
        self._pending_state: dict[int, tuple[_protocol.StateUpdate, float]] = {}
        # Ids whose current value carries a local-clock stamp (an undated
        # push or a raw edge). Local stamps are not comparable to the
        # M-SERV's `on` stamps, so `_supersedes` never compares them:
        # `_guarded` (received after the latest snapshot request) outranks
        # any seed, while an unguarded local stamp predates the request
        # and loses to the seed the request produced. `begin_refresh`
        # clears the guard; a server-stamped report clears both.
        self._local_stamped: set[int] = set()
        self._guarded: set[int] = set()
        # Endpoints whose reply mutates state, each reporting whether the
        # payload parsed. The rest are pure request/response, parsed by the
        # dispatcher with the endpoint's own `parses` gate and never sent
        # here.
        self._handlers: dict[str, Callable[[str, Applied], bool]] = {
            "details": partial(self._handle_catalogue, "devicesDetails"),
            "data_devices": partial(self._handle_catalogue, "data/devices"),
            "devices": self._handle_devices,
            "params_devices": self._handle_params_devices,
            "states": self._handle_states_snapshot,
            "info": self._handle_info,
        }
        # The endpoint table and this handler table are edited separately;
        # a name typo between them would otherwise surface as a silent
        # discovery hang, so misalignment fails construction instead.
        handler_gated = {ep.name for ep in _protocol.ENDPOINTS if ep.parses is None}
        if set(self._handlers) != handler_gated:
            raise RuntimeError(
                f"store handlers {sorted(self._handlers)} do not match the "
                f"handler-gated endpoints {sorted(handler_gated)}"
            )

    # --- routing ----------------------------------------------------------

    def begin_refresh(self) -> None:
        """Mark the start of a snapshot request cycle.

        Every value held now predates the snapshot the new cycle will
        deliver, so a dated seed may correct locally-stamped values again.
        The client calls this before it publishes the discovery requests.
        """
        self._guarded.clear()

    def apply_designer_metadata(self, resolved: dict[int, DesignerRecord]) -> Applied:
        """Hold the resolved designer table and fold it into known objects.

        ``location`` is authoritative from the record (None clears a stale
        name); ``matter_device_type`` refines and never clears - a record
        without a tag leaves the held value standing. The catalogue merge
        path (``_merge_metadata``) is where the column re-asserts itself.
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

    def apply_module_locations(self, by_mac: Mapping[int, str | None]) -> Applied:
        """Hold the swept module-level locations and fold them into modules.

        The value is authoritative per answering mac (None clears); a mac
        the sweep did not cover leaves both the held table and the module
        untouched.
        """
        applied = Applied()
        self._module_location_by_mac.update(by_mac)
        for mac, location in by_mac.items():
            mid = self._module_id_by_mac.get(mac)
            if mid is None:
                continue
            module = self.modules[mid]
            if module.location != location:
                module = replace(module, location=location)
                self.modules[mid] = module
                applied.events.append(ModuleUpdated(module))
        return applied

    def apply(self, msg: _protocol.Inbound) -> Applied:
        """Apply one typed message and report what it changed."""
        applied = Applied()
        match msg:
            case _protocol.EndpointReply(endpoint=endpoint, payload=body):
                # Only handler-gated replies reach the store - the dispatcher
                # parses pure request/response replies itself.
                applied.parsed = self._handlers[endpoint.name](body, applied)
            case _protocol.StateUpdate() as update:
                self._apply_state(update, applied)
            case _protocol.RawChannelEdge() as edge:
                self._apply_raw_channel(edge, applied)
            case _protocol.DiagnosticsReport(mac=mac, diagnostics=diagnostics):
                self._apply_diagnostics(mac, diagnostics, applied)
            case BusEvent() as event:
                applied.events.append(event)
        return applied

    # --- catalogues -------------------------------------------------------

    def _handle_catalogue(self, surface: str, payload: str, applied: Applied) -> bool:
        """Apply an object catalogue from either discovery surface.

        The two surfaces carry the same rows: the app-sync one simply omits
        ``params`` (which the ``params_devices`` table supplies instead) and
        ``stan_json``, so one merge covers both.
        """
        items = _protocol.parse_details(payload)
        if items is None:
            _LOGGER.warning("Could not parse Ampio %s catalogue", surface)
            return False
        touched = False
        for meta in items:
            touched |= self._merge_metadata(meta, applied)
        evicted = self._evict_missing_objects({meta.id for meta in items}, applied)
        if touched or evicted:
            self._rebuild_indexes(applied)
        return True

    def _evict_missing_objects(self, present: set[int], applied: Applied) -> bool:
        """Drop objects the authoritative catalogue no longer lists.

        Each tier's catalogue is complete for its account - the ``config``
        catalogue by being admin-only, the app-sync one because the grant
        bounds everything a restricted store could ever hold - so a reply's
        arrival is the authority to evict what it stopped listing, an empty
        reply included (a full grant revocation empties the app-sync view).
        """
        # The same completeness proves a buffered push's id will never gain
        # a catalogue row; without the prune, ghost-row pushes accumulate.
        for oid in list(self._pending_state):
            if oid not in present:
                del self._pending_state[oid]
        missing = [oid for oid in self.objects if oid not in present]
        if not missing:
            return False
        for oid in missing:
            obj = self.objects.pop(oid)
            self._params_by_id.pop(oid, None)
            self._stan_by_id.pop(oid, None)
            self._local_stamped.discard(oid)
            self._guarded.discard(oid)
            applied.events.append(ObjectRemoved(obj))
        return True

    def _merge_metadata(self, meta: _protocol.ObjectMetadata, applied: Applied) -> bool:
        """Fold one catalogue row into its object; True when anything changed.

        Only a real change is reported, so re-requesting the catalogue on every
        reconnect does not hand a consumer a full set of updates that say
        nothing new.
        """
        obj = self.objects.get(meta.id)
        created = obj is None
        if obj is None:
            obj = AmpioObject(id=meta.id)
        updates: dict[str, Any] = {
            name: getattr(meta, name) for name in _METADATA_FIELDS
        }
        # A row without the column leaves the params_devices value standing.
        if updates["params"] is None:
            updates["params"] = self._params_by_id.get(meta.id, obj.params)
        # The catalogue never carries the designer record's fields, so the
        # held table re-applies them on every merge - including the
        # re-creation after an eviction.
        designer = self._designer_by_id.get(meta.id)
        if designer is not None:
            updates["location"] = designer.location
            if designer.matter_device_type is not None:
                updates["matter_device_type"] = designer.matter_device_type
        changed = any(getattr(obj, name) != value for name, value in updates.items())
        updated = replace(obj, **updates)
        # A row without the column (the app-sync shape) falls back to the
        # buffered snapshot value, so reply order never decides whether an
        # object starts with its state.
        stan_json = (
            meta.stan_json
            if meta.stan_json is not None
            else self._stan_by_id.get(meta.id)
        )
        if stan_json is not None:
            updated, seeded = self._apply_stan_json(updated, stan_json)
            changed |= seeded
        # Replay a buffered push under the snapshot's dated-supersedes
        # rule; an undated push has no stamp comparable to the seed's
        # server clock and arrived live in this session, so it wins.
        pending = self._pending_state.pop(meta.id, None)
        if pending is not None:
            update, received_at = pending
            if update.on_ms is not None:
                stamp = float(update.on_ms) / 1000.0
                wins = self._supersedes(updated, stamp)
            else:
                stamp = received_at
                wins = True
                self._local_stamped.add(meta.id)
                self._guarded.add(meta.id)
            if wins:
                changed |= (
                    updated.value != update.value
                    or (
                        update.tilt is not None and updated.tilt_position != update.tilt
                    )
                    or (
                        update.thermostat is not None
                        and updated.thermostat != update.thermostat
                    )
                )
                updated = replace(
                    updated,
                    value=update.value,
                    tilt_position=(
                        update.tilt
                        if update.tilt is not None
                        else updated.tilt_position
                    ),
                    thermostat=(
                        update.thermostat
                        if update.thermostat is not None
                        else updated.thermostat
                    ),
                    updated_at=stamp,
                )
        self.objects[meta.id] = updated
        if created:
            # Existence is the news: a bare row dispatches too, and the
            # addition is the object's first event.
            applied.events.append(ObjectAdded(updated))
        elif changed:
            self._record(updated, applied)
        return changed or created

    def _handle_devices(self, payload: str, applied: Applied) -> bool:
        modules = _protocol.parse_devices(payload)
        if modules is None:
            _LOGGER.warning("Could not parse Ampio devices list")
            return False
        changed = False
        for module in modules:
            previous = self.modules.get(module.id)
            if previous is not None:
                module = replace(
                    module,
                    last_seen=previous.last_seen,
                    supply_voltage=previous.supply_voltage,
                    temperature=previous.temperature,
                )
            # The catalogue never carries the module-level location; the
            # held table re-applies it on every merge - including the
            # re-creation after an eviction.
            if module.mac is not None and module.mac in self._module_location_by_mac:
                module = replace(
                    module, location=self._module_location_by_mac[module.mac]
                )
            self.modules[module.id] = module
            # A new module or a changed catalogue row is news, exactly as an
            # object catalogue row is; the live fields were carried over
            # above, so any difference left is the catalogue's.
            if previous != module:
                changed = True
                applied.events.append(ModuleUpdated(module))
        # The module list is admin-only and complete, so its arrival is the
        # authority to evict what it stopped listing.
        present = {module.id for module in modules}
        missing = [mid for mid in self.modules if mid not in present]
        evicted = False
        for mid in missing:
            evicted = True
            applied.events.append(ModuleRemoved(self.modules.pop(mid)))
        if changed or evicted:
            self._rebuild_indexes(applied)
        return True

    def _handle_params_devices(self, payload: str, applied: Applied) -> bool:
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
                obj = replace(obj, params=params)
                self.objects[oid] = obj
                self._record(obj, applied)
        return True

    def _handle_info(self, payload: str, applied: Applied) -> bool:
        info = _protocol.parse_server_info(payload)
        if info is None:
            # Covers the identity-less reply too: without a server mac there
            # is nothing to scope a consumer's registry by, so it must
            # neither complete discovery nor displace an identified info.
            _LOGGER.warning("Could not parse Ampio info reply")
            return False
        previous = self.server_info
        # Warn when the version first becomes known or changes, not on the
        # re-request every reconnect issues.
        if previous is None or previous.server_version != info.server_version:
            _protocol.warn_if_below_baseline(info.server_version)
        self.server_info = info
        return True

    def _handle_states_snapshot(self, payload: str, applied: Applied) -> bool:
        entries = _protocol.parse_states_snapshot(payload)
        if entries is None:
            _LOGGER.warning("Could not parse Ampio states snapshot")
            return False
        self._stan_by_id = {
            entry.id: entry.stan_json
            for entry in entries
            if entry.stan_json is not None
        }
        for entry in entries:
            obj = self.objects.get(entry.id)
            if obj is None or entry.stan_json is None:
                # An id no catalogue established stays out of the store;
                # its value waits in _stan_by_id for the catalogue row
                # that may establish it.
                continue
            obj, changed = self._apply_stan_json(obj, entry.stan_json)
            self.objects[entry.id] = obj
            if changed:
                self._record(obj, applied)
        return True

    # --- live state -------------------------------------------------------

    def _apply_state(self, update: _protocol.StateUpdate, applied: Applied) -> None:
        obj = self.objects.get(update.id)
        if obj is None:
            self._pending_state[update.id] = (update, time.time())
            return
        if obj.raw_proven:
            # The raw path owns this object: the per-object echo repeats
            # what the raw edge delivered ~150 ms earlier, so it is
            # dropped whole. It still counts as live evidence of the
            # module.
            self._touch_module(obj.device_id)
            return
        stamp = (
            float(update.on_ms) / 1000.0 if update.on_ms is not None else time.time()
        )
        obj = replace(
            obj,
            value=update.value,
            tilt_position=update.tilt if update.tilt is not None else obj.tilt_position,
            thermostat=(
                update.thermostat if update.thermostat is not None else obj.thermostat
            ),
            updated_at=stamp,
        )
        self.objects[update.id] = obj
        if update.on_ms is None:
            self._local_stamped.add(update.id)
            self._guarded.add(update.id)
        else:
            self._local_stamped.discard(update.id)
            self._guarded.discard(update.id)
        self._touch_module(obj.device_id)
        self._record(obj, applied)

    def _apply_raw_channel(
        self, edge: _protocol.RawChannelEdge, applied: Applied
    ) -> None:
        oid = self._input_index.get((edge.mac, edge.prefix, edge.channel))
        if oid is None:
            return  # channel has no exposed Designer object - ignore
        obj = replace(
            self.objects[oid],
            raw_proven=True,
            value=edge.value,
            updated_at=time.time(),
        )
        self.objects[oid] = obj
        self._local_stamped.add(oid)
        self._guarded.add(oid)
        self._touch_module(obj.device_id)
        self._record(obj, applied)

    def _apply_diagnostics(
        self, mac: int, diagnostics: _protocol.ModuleDiagnostics, applied: Applied
    ) -> None:
        mid = self._module_id_by_mac.get(mac)
        if mid is None:
            return  # a module the catalogue does not list
        module = replace(
            self.modules[mid],
            supply_voltage=diagnostics.supply_voltage,
            temperature=diagnostics.temperature,
            last_seen=time.time(),
        )
        self.modules[mid] = module
        applied.events.append(ModuleUpdated(module))

    # --- helpers ----------------------------------------------------------

    def _apply_stan_json(
        self, obj: AmpioObject, stan_json: str
    ) -> tuple[AmpioObject, bool]:
        """Return `obj` with a bulk-snapshot value applied when it supersedes.

        The per-object topics are not retained, so this snapshot is the only
        resync after a reconnect - during which the object may well have
        changed. It must therefore be able to correct a stale value, while
        still losing to the live push that can arrive first on a fresh
        connection. The bool reports whether the visible state changed; the
        returned instance can differ even when it did not (a newer timestamp
        on the same value). A raw-proven object is skipped outright: its
        resync is the broker's retained raw table, and a DB snapshot may be
        staler than that raw truth with no comparable clock to prove it.
        """
        if obj.raw_proven:
            return obj, False
        seed = _protocol.parse_stan_json(stan_json)
        if seed is None:
            return obj, False
        reported_at = None if seed.on_ms is None else float(seed.on_ms) / 1000.0
        if seed.value is None or not self._supersedes(obj, reported_at):
            return obj, False
        changed = (
            obj.value != seed.value
            or (seed.tilt is not None and obj.tilt_position != seed.tilt)
            or (seed.thermostat is not None and obj.thermostat != seed.thermostat)
        )
        obj = replace(
            obj,
            value=seed.value,
            tilt_position=seed.tilt if seed.tilt is not None else obj.tilt_position,
            thermostat=seed.thermostat
            if seed.thermostat is not None
            else obj.thermostat,
            updated_at=reported_at,
        )
        self._local_stamped.discard(obj.id)
        self._guarded.discard(obj.id)
        return obj, changed

    def _supersedes(self, obj: AmpioObject, reported_at: float | None) -> bool:
        """Whether a dated snapshot report should replace what `obj` holds.

        Undated reports only fill a gap. Dated-versus-dated compares the
        M-SERV's own clock on both sides, so RTC skew cancels out. A
        locally-stamped value is never stamp-compared - this process's
        clock is not comparable to a server `on` stamp. Instead the
        snapshot request is the ordering boundary: a live value received
        after the latest request (guarded) outranks every seed, and one
        received before it loses to the seed that request produced.
        Raw-proven objects never reach this comparison: their snapshot
        rows are skipped before it.
        """
        if obj.value is None:
            return True
        if reported_at is None:
            return False
        if obj.updated_at is None:
            return True
        if obj.id in self._guarded:
            return False
        if obj.id in self._local_stamped:
            return True
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
            self.modules[module_id] = replace(module, last_seen=time.time())

    def _rebuild_indexes(self, applied: Applied) -> None:
        """Rebuild the routing tables for the raw tree.

        Both are keyed on the module's effective bus address (`mac`, the
        Designer override) - never `mac_global`, which diverges from the
        raw-topic MAC on replaced modules. `(mac, prefix, channel)` routes a
        raw channel to its object: the bridgeable input types, plus binary
        outputs (`przekaznik`) on the `o` prefix - a panel's status LEDs
        have no other retained surface, and every module's outputs share
        the channel shape. `mac` alone routes a module's own diagnostics
        broadcast.
        """
        index: dict[tuple[int, str, int], int] = {}
        for obj in self.objects.values():
            prefix = input_channel_prefix(obj.typ_komponentu)
            if prefix is None and obj.typ_komponentu == "przekaznik":
                prefix = "o"
            if prefix is None or obj.funkcja is None or obj.device_id is None:
                continue
            module = self.modules.get(obj.device_id)
            if module is None or module.mac is None:
                continue
            index[(module.mac, prefix, obj.funkcja)] = obj.id
        self._input_index = index
        by_mac: dict[int, int] = {}
        colliding: set[int] = set()
        for module in self.modules.values():
            if module.mac is None:
                continue
            if module.mac in by_mac:
                colliding.add(module.mac)
            by_mac[module.mac] = module.id
        self._module_id_by_mac = by_mac
        if frozenset(colliding) != self.colliding_macs:
            self.colliding_macs = frozenset(colliding)
            if colliding:
                _LOGGER.warning(
                    "Ampio modules share the override mac(s) %s; raw edges "
                    "and diagnostics on a shared mac cannot be attributed "
                    "reliably - give each module a unique mac in Designer",
                    sorted(colliding),
                )
        # An object the index no longer covers must go back to its per-object
        # updates, or a mac change in Designer would freeze it for good. The
        # flip is public state, so it dispatches like any other change.
        covered = set(index.values())
        for oid, obj in self.objects.items():
            if obj.raw_proven and oid not in covered:
                obj = replace(obj, raw_proven=False)
                self.objects[oid] = obj
                self._record(obj, applied)

    def _record(self, obj: AmpioObject, applied: Applied) -> None:
        applied.events.append(ObjectUpdated(obj))


# The catalogue-owned object fields, derived from the wire row's own shape
# so a new column is added in one place and flows through the merge; `id`
# keys the merge and `stan_json` seeds state, so neither is metadata.
_METADATA_FIELDS = tuple(
    f.name
    for f in fields(_protocol.ObjectMetadata)
    if f.name not in ("id", "stan_json")
)
