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
from typing import Any

from . import _protocol
from .classification import input_channel_prefix
from .errors import AmpioProtocolError
from .events import (
    BusEventRaised,
    ModuleRemoved,
    ModuleUpdated,
    ObjectAdded,
    ObjectRemoved,
    ObjectUpdated,
    StoreEvent,
)
from .models import (
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioServerInfo,
    DesignerRecord,
    ModuleRecord,
    PanelSettings,
    leaf_mac,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Applied:
    """What one inbound message did to the store."""

    # Everything the message changed, in processing order, ready to dispatch.
    # Update events carry a snapshot taken as the change was applied, so a
    # consumer that defers processing still sees the state the event was
    # about; removal events carry final state already gone from the store.
    events: list[StoreEvent] = field(default_factory=list)


class AmpioStore:
    """Applies typed M-SERV messages to the object, module and server state.

    The account tier decides which surfaces answer (docs/account-tiers.md),
    so the store holds the handlers of that tier alone. Each fact then has
    one source: the admin catalogue carries the Designer config columns
    inline, and on the app-sync tier `data/params_devices` carries them.
    """

    def __init__(self, tier: AccessTier) -> None:
        self._tier = tier
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
        # Full-catalogue per-object config facts (`params`, `czas`, `url`)
        # from `data/params_devices`, the app-sync tier's one source for
        # them. Held because the two app-sync replies arrive in no fixed
        # order, and re-applied on every merge - an eviction included.
        # Stays empty on the admin tier, which is served neither the table
        # nor a catalogue that needs it.
        self._params_by_id: dict[int, _protocol.ParamsEntry] = {}
        # Whether the config table has answered at least once, so a gap in
        # its coverage is told apart from a table still in flight.
        self._params_received = False
        # Granted objects the config table carries no row for. The table
        # covers the full catalogue, so a non-empty set is a server fault:
        # those objects read every Designer config flag as unset. Warned
        # once per change and surfaced for diagnostics.
        self.missing_params_ids: frozenset[int] = frozenset()
        # `{object_id: DesignerRecord}` accumulated across resolve
        # sweeps (a sweep updates its joined ids and leaves the rest),
        # kept so a catalogue refresh re-applies what the CAN records
        # proved (the catalogue itself never carries them).
        self._record_by_id: dict[int, DesignerRecord] = {}
        # `{mac: ModuleRecord}` accumulated across sweeps, kept for the
        # same reason on the module side; an empty bundle is an
        # authoritative "answered, unassigned".
        self._module_record_by_mac: dict[int, ModuleRecord] = {}
        # `{mac: {function id: channel count}}` accumulated across sweeps,
        # kept for the same reason; an empty map is an authoritative
        # "answered, advertising nothing".
        self._module_capabilities_by_mac: dict[int, Mapping[int, int]] = {}
        # `{mac: PanelSettings}` accumulated across sweeps, kept for the
        # same reason. A module absent here is one whose panel layout the
        # sweep could not resolve, which includes everything that is not a
        # touch panel.
        self._panel_settings_by_mac: dict[int, PanelSettings] = {}
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
        # This tier's endpoints whose reply mutates state. The rest are pure
        # request/response, parsed by the dispatcher with the endpoint's own
        # `parses` gate and never sent here.
        self._handlers: dict[str, Callable[[str, Applied], None]] = {
            "states": self._handle_states_snapshot,
            "info": self._handle_info,
        }
        if tier is AccessTier.ADMIN:
            self._handlers["details"] = self._handle_admin_catalogue
            self._handlers["devices"] = self._handle_devices
        else:
            self._handlers["data_devices"] = self._handle_app_sync_catalogue
            self._handlers["params_devices"] = self._handle_params_devices
        # The endpoint table and this handler table are edited separately;
        # a name typo between them would otherwise surface as a silent
        # discovery hang, so misalignment fails construction instead.
        handler_gated = {
            ep.name
            for ep in _protocol.ENDPOINTS
            if ep.parses is None and ep.tier in (None, tier)
        }
        if set(self._handlers) != handler_gated:
            raise RuntimeError(
                f"store handlers {sorted(self._handlers)} do not match the "
                f"{tier.value} tier's handler-gated endpoints "
                f"{sorted(handler_gated)}"
            )

    # --- routing ----------------------------------------------------------

    def begin_refresh(self) -> None:
        """Mark the start of a snapshot request cycle.

        Every value held now predates the snapshot the new cycle will
        deliver, so a dated seed may correct locally-stamped values again.
        The client calls this before it publishes the discovery requests.
        """
        self._guarded.clear()

    def apply_designer_records(self, resolved: dict[int, DesignerRecord]) -> Applied:
        """Hold the swept record entries and fold them into known objects.

        A joined object's ``record`` is replaced wholesale - the entry is
        what its module answered, None fields included. Objects a sweep
        did not join keep their previous record, and the held table
        accumulates across sweeps so a catalogue re-seed re-folds
        everything this session learned.
        """
        applied = Applied()
        self._record_by_id.update(resolved)
        for oid, rec in resolved.items():
            obj = self.objects.get(oid)
            if obj is None or obj.record == rec:
                continue
            obj = replace(obj, record=rec)
            self.objects[oid] = obj
            self._record(obj, applied)
        return applied

    def apply_module_sweep(
        self,
        records: Mapping[int, ModuleRecord],
        capabilities: Mapping[int, Mapping[int, int]],
        panel_settings: Mapping[int, PanelSettings],
    ) -> Applied:
        """Hold one sweep's module facts and fold them into modules.

        Both maps come from the same ``device_api`` reply, so they fold
        together and a module changed by either reports one event.
        Wholesale per answering mac, exactly as the object side; a mac
        the sweep did not cover leaves both the held tables and the
        module untouched.
        """
        applied = Applied()
        self._module_record_by_mac.update(records)
        self._module_capabilities_by_mac.update(capabilities)
        self._panel_settings_by_mac.update(panel_settings)
        for mac in {*records, *capabilities, *panel_settings}:
            mid = self._module_id_by_mac.get(mac)
            if mid is None:
                continue
            module = self.modules[mid]
            updated = module
            if mac in records and updated.record != records[mac]:
                updated = replace(updated, record=records[mac])
            if mac in capabilities and updated.capabilities != capabilities[mac]:
                updated = replace(updated, capabilities=capabilities[mac])
            if mac in panel_settings and updated.panel_settings != panel_settings[mac]:
                updated = replace(updated, panel_settings=panel_settings[mac])
            if updated is not module:
                self.modules[mid] = updated
                applied.events.append(ModuleUpdated(updated))
        return applied

    def apply(self, msg: _protocol.Inbound, *, retained: bool = False) -> Applied:
        """Apply one typed message and report what it changed.

        ``retained`` marks a broker replay from its retained store. A
        replay carries the value but says nothing about whether the
        module is alive now, so it never touches ``last_seen``.
        """
        applied = Applied()
        match msg:
            case _protocol.EndpointReply(endpoint=endpoint, payload=body):
                # Only handler-gated replies reach the store - the dispatcher
                # parses pure request/response replies itself.
                handler = self._handlers.get(endpoint.name)
                if handler is None:
                    raise AmpioProtocolError(
                        f"The Ampio {endpoint.name!r} reply is not served on "
                        f"the {self._tier.value} tier"
                    )
                handler(body, applied)
            case _protocol.StateUpdate() as update:
                self._apply_state(update, applied)
            case _protocol.RawChannelEdge() as edge:
                self._apply_raw_channel(edge, applied, retained=retained)
            case _protocol.DiagnosticsReport(mac=mac, diagnostics=diagnostics):
                self._apply_diagnostics(mac, diagnostics, applied, retained=retained)
            case BusEventRaised() as event:
                applied.events.append(event)
        return applied

    # --- catalogues -------------------------------------------------------

    def _handle_admin_catalogue(self, payload: str, applied: Applied) -> None:
        """Apply a `config/devicesDetails` reply, the admin object catalogue.

        Every row carries the Designer config columns inline, so the row is
        their one source on this tier.
        """
        served = _protocol.parse_details(payload)
        self._apply_catalogue(
            [row.shared for row in served],
            {
                row.shared.id: {
                    "params": row.params,
                    "czas": row.czas,
                    "url": row.url,
                }
                for row in served
            },
            applied,
        )

    def _handle_app_sync_catalogue(self, payload: str, applied: Applied) -> None:
        """Apply a `data/devices` reply, the grant-filtered app-sync catalogue.

        The surface serves no Designer config columns, so the held
        `data/params_devices` table is their one source here. The table
        re-applies on every merge, the re-creation after an eviction
        included.
        """
        served = _protocol.parse_app_sync_devices(payload)
        self._apply_catalogue(served, self._held_config(served), applied)
        self._report_params_coverage()

    def _held_config(
        self, served: list[_protocol.ObjectMetadata]
    ) -> dict[int, Mapping[str, Any]]:
        """The config columns the held table holds for the served rows."""
        return {
            meta.id: {"params": entry.params, "czas": entry.czas, "url": entry.url}
            for meta in served
            if (entry := self._params_by_id.get(meta.id)) is not None
        }

    def _apply_catalogue(
        self,
        served: list[_protocol.ObjectMetadata],
        config: Mapping[int, Mapping[str, Any]],
        applied: Applied,
    ) -> None:
        """Fold one tier's whole object catalogue into the store.

        ``config`` carries the Designer config columns per object id, from
        whichever source this tier serves them on. An id absent from it is
        one whose columns have not arrived yet, which leaves the object
        reading the unset values until they do.
        """
        # One reply is the whole catalogue this tier holds, so its leafed
        # rows are every sibling a leafless row can learn its module from.
        sibling_macs: dict[int, int] = {}
        for meta in served:
            mac = leaf_mac(meta.leaf_id)
            if mac is not None:
                sibling_macs[meta.id_urzadzenia] = mac
        touched = False
        for meta in served:
            touched |= self._merge_metadata(
                meta, config.get(meta.id, {}), sibling_macs, applied
            )
        evicted = self._evict_missing_objects({meta.id for meta in served}, applied)
        if touched or evicted:
            self._rebuild_indexes(applied)

    def _evict_missing_objects(self, present: set[int], applied: Applied) -> bool:
        """Drop objects the authoritative catalogue no longer lists.

        Each tier's catalogue is complete for its account - the ``config``
        catalogue by being admin-only, the app-sync one because the grant
        bounds everything a restricted store could ever hold - so a reply's
        arrival is the authority to evict what it stopped listing, an empty
        reply included (a full grant revocation empties the app-sync view).
        """
        # The same completeness proves a buffered push's id will never gain
        # a catalogue row; without the prune, pushes for such ids accumulate.
        for oid in list(self._pending_state):
            if oid not in present:
                del self._pending_state[oid]
        missing = [oid for oid in self.objects if oid not in present]
        if not missing:
            return False
        for oid in missing:
            obj = self.objects.pop(oid)
            # The held config table stays whole: it is not grant-filtered,
            # and it is this tier's one source for the Designer config
            # columns, so a re-granted object reads them again at once.
            self._stan_by_id.pop(oid, None)
            self._local_stamped.discard(oid)
            self._guarded.discard(oid)
            applied.events.append(ObjectRemoved(obj))
        return True

    def _merge_metadata(
        self,
        meta: _protocol.ObjectMetadata,
        config: Mapping[str, Any],
        sibling_macs: Mapping[int, int],
        applied: Applied,
    ) -> bool:
        """Fold one catalogue row into its object; True when anything changed.

        Only a real change is reported, so re-requesting the catalogue on every
        reconnect does not hand a consumer a full set of updates that say
        nothing new.
        """
        obj = self.objects.get(meta.id)
        created = obj is None
        if obj is None:
            obj = AmpioObject(
                id=meta.id,
                id_urzadzenia=meta.id_urzadzenia,
                typ_komponentu=meta.typ_komponentu,
                interpretacja=meta.interpretacja,
                funkcja=meta.funkcja,
            )
        updates: dict[str, Any] = {
            name: getattr(meta, name) for name in _METADATA_FIELDS
        }
        updates["sibling_module_mac"] = sibling_macs.get(meta.id_urzadzenia)
        updates.update(config)
        # The catalogue never carries the record entry, so the held table
        # re-applies it on every merge - including the re-creation after
        # an eviction.
        record = self._record_by_id.get(meta.id)
        if record is not None:
            updates["record"] = record
        changed = any(getattr(obj, name) != value for name, value in updates.items())
        updated = replace(obj, **updates)
        # The states snapshot is the one seed source on both tiers, so a
        # buffered snapshot value applies here and reply order never decides
        # whether an object starts with its state.
        stan_json = self._stan_by_id.get(meta.id)
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
                    updated.state != update.state
                    or (update.lammel is not None and updated.lammel != update.lammel)
                    or (update.block is not None and updated.block != update.block)
                    or (
                        update.thermostat is not None
                        and updated.thermostat != update.thermostat
                    )
                )
                updated = replace(
                    updated,
                    state=update.state,
                    lammel=(
                        update.lammel if update.lammel is not None else updated.lammel
                    ),
                    block=update.block if update.block is not None else updated.block,
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

    def _handle_devices(self, payload: str, applied: Applied) -> None:
        modules = _protocol.parse_devices(payload)
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
            # The catalogue never carries the record entry; the held
            # table re-applies it on every merge - including the
            # re-creation after an eviction.
            mac = module.mac
            if mac is not None:
                if mac in self._module_record_by_mac:
                    module = replace(module, record=self._module_record_by_mac[mac])
                if mac in self._module_capabilities_by_mac:
                    module = replace(
                        module, capabilities=self._module_capabilities_by_mac[mac]
                    )
                if mac in self._panel_settings_by_mac:
                    module = replace(
                        module, panel_settings=self._panel_settings_by_mac[mac]
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

    def _handle_params_devices(self, payload: str, applied: Applied) -> None:
        """Apply the ``data/params_devices`` config table.

        The app-sync tier's one source for `params`, `czas` and `url`. The
        whole table is held for catalogue rows that arrive later, and
        objects already known are updated in place. An id with no known
        object creates no placeholder: the table is not grant-filtered, so
        most of it refers to objects the account cannot otherwise see.
        """
        self._params_by_id = _protocol.parse_params_devices(payload)
        self._params_received = True
        for oid, entry in self._params_by_id.items():
            obj = self.objects.get(oid)
            if obj is not None and (
                obj.params != entry.params
                or obj.czas != entry.czas
                or obj.url != entry.url
            ):
                obj = replace(obj, params=entry.params, czas=entry.czas, url=entry.url)
                self.objects[oid] = obj
                self._record(obj, applied)
        self._report_params_coverage()

    def _report_params_coverage(self) -> None:
        """Name the granted objects the config table carries no row for.

        The table covers the whole object catalogue, so every object a
        grant lists has a row. A gap leaves those objects reading every
        Designer config flag as unset, which is a server fault to report
        rather than a state to model. Warned once per change, and held for
        diagnostics either way.
        """
        if not self._params_received:
            return
        missing = frozenset(self.objects) - self._params_by_id.keys()
        if missing == self.missing_params_ids:
            return
        self.missing_params_ids = missing
        if missing:
            _LOGGER.warning(
                "The Ampio params_devices table carries no row for object(s) "
                "%s; every Designer config flag of theirs reads as unset",
                sorted(missing),
            )

    def _handle_info(self, payload: str, applied: Applied) -> None:
        """Apply a `data/info` reply, the M-SERV's self-report.

        The reply names the asking account, which is the wire's own verdict
        on the tier. The username decided the same question at
        construction, and every subscription and request follows from that
        decision, so a disagreement means the session is aimed at the wrong
        surfaces. Nothing in the reply can fix that, so it is refused.
        """
        info = _protocol.parse_server_info(payload)
        if info.access_tier is not self._tier:
            raise AmpioProtocolError(
                f"The Ampio server reports account id {info.user_id}, the "
                f"{info.access_tier.value} tier, for a session connected on "
                f"the {self._tier.value} tier"
            )
        previous = self.server_info
        # Warn when the version first becomes known or changes, not on the
        # re-request every reconnect issues.
        if previous is None or previous.server_version != info.server_version:
            _protocol.warn_if_below_baseline(info.server_version)
        self.server_info = info

    def _handle_states_snapshot(self, payload: str, applied: Applied) -> None:
        """Apply a `data/states` reply, the one initial-value source.

        The snapshot answers both tiers, so neither catalogue needs to seed
        a value. An id no catalogue established stays out of the store, and
        its value waits here for the catalogue row that may establish it.
        """
        entries = _protocol.parse_states_snapshot(payload)
        self._stan_by_id = {entry.id: entry.stan_json for entry in entries}
        for entry in entries:
            obj = self.objects.get(entry.id)
            if obj is None:
                continue
            obj, changed = self._apply_stan_json(obj, entry.stan_json)
            self.objects[entry.id] = obj
            if changed:
                self._record(obj, applied)

    # --- live state -------------------------------------------------------

    def _apply_state(self, update: _protocol.StateUpdate, applied: Applied) -> None:
        obj = self.objects.get(update.id)
        if obj is None:
            self._pending_state[update.id] = (update, time.time())
            return
        if obj.raw_owned:
            # The raw path owns this object: the per-object echo repeats
            # what the raw edge delivered ~150 ms earlier, so it is
            # dropped whole. It still counts as live evidence of the
            # module.
            self._touch_module(obj.id_urzadzenia)
            return
        stamp = (
            float(update.on_ms) / 1000.0 if update.on_ms is not None else time.time()
        )
        obj = replace(
            obj,
            state=update.state,
            lammel=update.lammel if update.lammel is not None else obj.lammel,
            block=update.block if update.block is not None else obj.block,
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
        self._touch_module(obj.id_urzadzenia)
        self._record(obj, applied)

    def _apply_raw_channel(
        self, edge: _protocol.RawChannelEdge, applied: Applied, *, retained: bool
    ) -> None:
        oid = self._input_index.get((edge.mac, edge.prefix, edge.channel))
        if oid is None:
            return  # channel has no exposed Designer object - ignore
        obj = replace(
            self.objects[oid],
            raw_owned=True,
            state=edge.state,
            updated_at=time.time(),
        )
        self.objects[oid] = obj
        self._local_stamped.add(oid)
        self._guarded.add(oid)
        if not retained:
            self._touch_module(obj.id_urzadzenia)
        self._record(obj, applied)

    def _apply_diagnostics(
        self,
        mac: int,
        diagnostics: _protocol.ModuleDiagnostics,
        applied: Applied,
        *,
        retained: bool,
    ) -> None:
        mid = self._module_id_by_mac.get(mac)
        if mid is None:
            return  # a module the catalogue does not list
        previous = self.modules[mid]
        module = replace(
            previous,
            supply_voltage=diagnostics.supply_voltage,
            temperature=diagnostics.temperature,
            last_seen=previous.last_seen if retained else time.time(),
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
        on the same value). A raw-owned object is skipped outright: its
        resync is the broker's retained raw table, and a DB snapshot may be
        staler than that raw truth with no comparable clock to prove it.
        """
        if obj.raw_owned:
            return obj, False
        seed = _protocol.parse_stan_json(stan_json)
        reported_at = float(seed.on_ms) / 1000.0
        if not self._supersedes(obj, reported_at):
            return obj, False
        changed = (
            obj.state != seed.state
            or (seed.lammel is not None and obj.lammel != seed.lammel)
            or (seed.block is not None and obj.block != seed.block)
            or (seed.thermostat is not None and obj.thermostat != seed.thermostat)
        )
        obj = replace(
            obj,
            state=seed.state,
            lammel=seed.lammel if seed.lammel is not None else obj.lammel,
            block=seed.block if seed.block is not None else obj.block,
            thermostat=seed.thermostat
            if seed.thermostat is not None
            else obj.thermostat,
            updated_at=reported_at,
        )
        self._local_stamped.discard(obj.id)
        self._guarded.discard(obj.id)
        return obj, changed

    def _supersedes(self, obj: AmpioObject, reported_at: float) -> bool:
        """Whether a snapshot report should replace what `obj` holds.

        Every snapshot row carries the M-SERV stamp it was reported at, so
        stamp-versus-stamp compares that one clock on both sides and RTC
        skew cancels out. A locally-stamped value is never stamp-compared -
        this process's clock is not comparable to a server `on` stamp.
        Instead the snapshot request is the ordering boundary: a live value
        received after the latest request (guarded) outranks every seed, and
        one received before it loses to the seed that request produced. A
        value with no stamp of its own is one nothing has reported yet.
        Raw-owned objects never reach this comparison: their snapshot rows
        are skipped before it.
        """
        if obj.state is None:
            return True
        if obj.updated_at is None:
            return True
        if obj.id in self._guarded:
            return False
        if obj.id in self._local_stamped:
            return True
        return reported_at >= obj.updated_at

    def _touch_module(self, module_id: int) -> None:
        """Mark the module as having produced live evidence just now.

        One clock only: the local receive time, because a live message is by
        definition received "now". Snapshot and catalogue seeds do not touch
        this - they replay DB state that may be arbitrarily old, which says
        nothing about whether the module is alive. An id the module list
        does not carry touches nothing, which on the reference install is
        the soft-deleted rows alone.
        """
        module = self.modules.get(module_id)
        if module is not None:
            self.modules[module_id] = replace(module, last_seen=time.time())

    def _rebuild_indexes(self, applied: Applied) -> None:
        """Rebuild the routing tables for the raw tree.

        Both are keyed on the module's effective bus address (`mac`, the
        Designer override) - never `mac_global`, which diverges from the
        raw-topic MAC on replaced modules. `(mac, prefix, channel)` routes a
        raw channel to its object: the bridgeable input types, plus
        `przekaznik` outputs on the `o` prefix, or on `a` for an
        open-collector leaf - a panel's status LEDs have no other retained
        surface, an OC output never echoes on its object topic, and every
        module's outputs share the channel shape. `mac` alone routes a
        module's own diagnostics broadcast.
        """
        index: dict[tuple[int, str, int], int] = {}
        for obj in self.objects.values():
            prefix = input_channel_prefix(obj.typ_komponentu)
            if prefix is None and obj.typ_komponentu == "przekaznik":
                # A binary output reports on `o`; an open-collector output
                # (leaf class 67) reports a u8 on `a`, same 1-based channel.
                prefix = "a" if obj.sf_id == _protocol.OC_OUTPUT_SF else "o"
            if prefix is None:
                continue
            module = self.modules.get(obj.id_urzadzenia)
            if module is None:
                continue
            index[(module.mac, prefix, obj.funkcja)] = obj.id
        self._input_index = index
        by_mac: dict[int, int] = {}
        colliding: set[int] = set()
        for module in self.modules.values():
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
            if obj.raw_owned and oid not in covered:
                obj = replace(obj, raw_owned=False)
                self.objects[oid] = obj
                self._record(obj, applied)

    def _record(self, obj: AmpioObject, applied: Applied) -> None:
        applied.events.append(ObjectUpdated(obj))


# The object fields both catalogue surfaces own, derived from the shared
# row's own shape so a new column is added in one place and flows through
# the merge. `id` keys the merge, so it is not metadata. The Designer
# config columns are not here: each tier serves them from its own surface,
# and the merge takes them as `config`.
_METADATA_FIELDS = tuple(
    f.name for f in fields(_protocol.ObjectMetadata) if f.name != "id"
)
