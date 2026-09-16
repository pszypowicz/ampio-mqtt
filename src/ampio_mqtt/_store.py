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
    NotConfigured,
    ObjectAdded,
    ObjectRemoved,
    ObjectUpdated,
    PresenceChanged,
    StoreEvent,
)
from .models import (
    HIDDEN_FLAG,
    AccessTier,
    AmpioModule,
    AmpioObject,
    AmpioServerInfo,
    CoverParameters,
    DesignerRecord,
    ModuleAddress,
    ModuleRecord,
    PanelSettings,
    PresenceDetection,
    PresenceSimulation,
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
    one source: `data/params_devices` carries them on both tiers.
    """

    def __init__(self, tier: AccessTier) -> None:
        self._tier = tier
        self.objects: dict[int, AmpioObject] = {}
        self.modules: dict[int, AmpioModule] = {}
        self.server_info: AmpioServerInfo | None = None
        # The two rows the M-SERV creates itself, routed here instead of
        # `objects`. None until the catalogue lists the row, and None while
        # the row carries the hidden bit. docs/presence.md.
        self.presence_detection: PresenceDetection | None = None
        self.presence_simulation: PresenceSimulation | None = None
        # The detection row's home-status code, held whether or not the row
        # is visible. A live push sets it and marks the refresh cycle, so a
        # snapshot after a reconnect corrects a code no push has replaced
        # since the cycle began.
        self._home_status: int | None = None
        self._detection_pushed = False
        # The catalogue rows behind the two attributes, by object id, so a
        # params push can rebuild them and a state push can find the
        # detection row.
        self._presence_rows: dict[int, _protocol.ObjectMetadata] = {}
        # The last `data/devices` reply, held whole so a `data/params_devices`
        # push alone re-runs the door on it. None until the first reply.
        self._catalogue: list[_protocol.ObjectMetadata] | None = None
        # The `(id, name)` pairs of the rows the door left out because they
        # carry no leaf, from the last apply. Empty when every listed row
        # was admitted or hidden. The client raises and reports from it.
        self.not_configured: tuple[tuple[int, str | None], ...] = ()
        # Override macs shared by two or more catalogue rows. The raw
        # routing tables are keyed by mac, so edges and diagnostics on a
        # colliding mac cannot be attributed reliably; the collision is
        # warned once per change and surfaced for diagnostics.
        self.colliding_macs: frozenset[int] = frozenset()
        # Raw-channel bridge: (module mac, prefix, channel) -> the ids of
        # every object on that channel.
        self._input_index: dict[tuple[int, str, int], tuple[int, ...]] = {}
        # Effective bus mac -> module id, for routing a module's own
        # broadcasts. Ids, not instances: modules are frozen and replaced on
        # every change, so a cached instance would go stale.
        self._module_id_by_mac: dict[int, int] = {}
        # Full-catalogue per-object config facts (`params`, `czas`, `url`)
        # from `data/params_devices`, this store's one source for them on
        # both tiers. Held because the two catalogue replies arrive in no
        # fixed order, and re-applied on every merge - an eviction included.
        self._params_by_id: dict[int, _protocol.ParamsEntry] = {}
        # Whether the config table has answered at least once, so a gap in
        # its coverage is told apart from a table still in flight.
        self._params_received = False
        # Catalogue objects the params table carries no row for. Every
        # object the catalogue lists has a row on both tiers, so a
        # non-empty set is a server fault: those objects read every
        # Designer config flag as unset. Warned once per change and
        # surfaced for diagnostics.
        self.missing_params_ids: frozenset[int] = frozenset()
        # `{object_id: DesignerRecord}` accumulated across resolve
        # sweeps (a sweep updates its joined ids and leaves the rest),
        # kept so a catalogue refresh re-applies what the CAN records
        # proved (the catalogue itself never carries them).
        self._record_by_id: dict[int, DesignerRecord] = {}
        # `{object_id: CoverParameters}` accumulated across sweeps, kept
        # for the same reason as the record table: the catalogue never
        # carries the params blob either.
        self._cover_parameters_by_id: dict[int, CoverParameters] = {}
        # `{object_id: bool}` accumulated across sweeps, on the same terms.
        # False is an answer, so absence is the only unresolved state.
        self._lock_support_by_id: dict[int, bool] = {}
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
        # `{object_id: seed}` from the last `data/states` snapshot,
        # kept for the same reason; a snapshot row for an id no catalogue
        # established creates nothing.
        self._stan_by_id: dict[int, _protocol.StanJsonSeed] = {}
        # Latest live push per id no catalogue has established. Only the
        # catalogues decide which objects exist, so a push that races ahead
        # of them waits here and surfaces with the catalogue row.
        self._pending_state: dict[int, _protocol.StateUpdate] = {}
        # Ids whose current value carries a local-clock stamp, which only a
        # raw channel edge produces: the raw tree carries no stamp of its
        # own. Local stamps are not comparable to the M-SERV's `on` stamps,
        # so `_supersedes` never compares them:
        # `_guarded` (received after the latest snapshot request) outranks
        # any seed, while an unguarded local stamp predates the request
        # and loses to the seed the request produced. `begin_refresh`
        # clears the guard; a server-stamped report clears both.
        self._local_stamped: set[int] = set()
        self._guarded: set[int] = set()
        # The broker replays its retained raw tree within a second of the
        # subscribe, before any catalogue reply, so the routing tables above
        # are still empty when those frames land. They wait here, keyed the
        # way the tables key them, and `_rebuild_indexes` folds them in once
        # the catalogue builds the routing. Both stay empty on the app-sync
        # tier, which is served no raw tree.
        self._pending_raw: dict[tuple[int, str, int], str] = {}
        self._pending_diagnostics: dict[int, _protocol.ModuleDiagnostics] = {}
        # This tier's endpoints whose reply mutates state. The rest are pure
        # request/response, parsed by the dispatcher with the endpoint's own
        # `parses` gate and never sent here.
        self._handlers: dict[str, Callable[[Mapping[str, Any], Applied], None]] = {
            "states": self._handle_states_snapshot,
            "info": self._handle_info,
            "data_devices": self._handle_catalogue,
            "params_devices": self._handle_params_devices,
        }
        if tier is AccessTier.ADMIN:
            self._handlers["devices"] = self._handle_devices
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
        deliver, so a dated seed may correct locally-stamped values again,
        and a detection code the coming snapshot carries may correct one no
        push has replaced since. The client calls this before it publishes
        the discovery requests.
        """
        self._guarded.clear()
        self._detection_pushed = False

    def apply_designer_records(
        self,
        resolved: Mapping[int, DesignerRecord],
        cover_parameters: Mapping[int, CoverParameters],
        lock_support: Mapping[int, bool],
    ) -> Applied:
        """Hold one sweep's per-object facts and fold them into known objects.

        A joined object's ``record`` is replaced wholesale - the entry is
        what its module answered, None fields included. All three maps
        come from one ``device_api`` reply, so an object changed by any of
        them reports one event. Objects a sweep did not join keep what
        they had, and the held tables accumulate across sweeps so a
        catalogue re-seed re-folds everything this session learned. A row
        that left the roller class is the exception: the merge drops its
        cover parameters and its lock support, facts only a roller kind
        carries.
        """
        applied = Applied()
        self._record_by_id.update(resolved)
        self._cover_parameters_by_id.update(cover_parameters)
        self._lock_support_by_id.update(lock_support)
        # Insertion order, so the event order follows the maps rather than
        # a set's hash order.
        for oid in dict.fromkeys((*resolved, *cover_parameters, *lock_support)):
            obj = self.objects.get(oid)
            if obj is None:
                continue
            updated = obj
            if oid in resolved and updated.record != resolved[oid]:
                updated = replace(updated, record=resolved[oid])
            if (
                oid in cover_parameters
                and updated.cover_parameters != cover_parameters[oid]
            ):
                updated = replace(updated, cover_parameters=cover_parameters[oid])
            if oid in lock_support and updated.block_writable != lock_support[oid]:
                updated = replace(updated, block_writable=lock_support[oid])
            if updated is not obj:
                self.objects[oid] = updated
                self._record(updated, applied)
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
            case _protocol.StateUpdate() as update:
                self._apply_state(update, applied)
            case _protocol.RawChannelEdge() as edge:
                self._apply_raw_channel(edge, applied, retained=retained)
            case _protocol.ColorTempFrame(edges=edges):
                for edge in edges:
                    self._apply_raw_channel(edge, applied, retained=retained)
            case _protocol.DiagnosticsReport(mac=mac, diagnostics=diagnostics):
                self._apply_diagnostics(mac, diagnostics, applied, retained=retained)
            case BusEventRaised() as event:
                applied.events.append(event)
        return applied

    def apply_endpoint(
        self, endpoint: _protocol.Endpoint, data: Mapping[str, Any]
    ) -> Applied:
        """Apply one decoded table reply and report what it changed.

        Takes the decoded envelope rather than the reply bytes, because the
        dispatcher decodes once for the retained summary and this handler
        both. Only handler-gated replies reach the store - the dispatcher
        parses a pure request/response reply itself.
        """
        applied = Applied()
        handler = self._handlers.get(endpoint.name)
        if handler is None:
            raise AmpioProtocolError(
                f"The Ampio {endpoint.name!r} reply is not served on "
                f"the {self._tier.value} tier"
            )
        handler(data, applied)
        return applied

    # --- catalogues -------------------------------------------------------

    def _handle_catalogue(self, data: Mapping[str, Any], applied: Applied) -> None:
        """Apply a `data/devices` reply through the door, then hold it.

        The door runs once the params table is in hand, and the reply is
        held only after the door admitted it, so a refused reply leaves
        every held field as it was.
        """
        served = _protocol.parse_app_sync_devices(data)
        if self._params_received:
            self._apply_catalogue(
                served, self._config_for(served, self._params_by_id), applied
            )
        self._catalogue = served
        # The reply is the whole catalogue this account holds, so a buffered
        # push for an id it does not list will never gain a row. The params
        # reply of the pair runs the door on the older held catalogue, which
        # is why only this reply may prune.
        listed = {meta.id for meta in served}
        for oid in list(self._pending_state):
            if oid not in listed:
                del self._pending_state[oid]
        self._report_params_coverage()

    def _handle_params_devices(self, data: Mapping[str, Any], applied: Applied) -> None:
        """Apply the `data/params_devices` table through the door, then hold it.

        The table is this store's one source for `params`, `czas` and `url`
        on both tiers, the hidden bit included, so the door waits for it. A
        push of the table alone re-runs the door on the held catalogue: a
        hidden bit that changes evicts or admits its row, and the two
        presence rows settle from the same table. The table is held only
        after the door admitted the result.
        """
        params = _protocol.parse_params_devices(data)
        if self._catalogue is not None:
            self._apply_catalogue(
                self._catalogue, self._config_for(self._catalogue, params), applied
            )
        self._params_by_id = params
        self._params_received = True
        self._report_params_coverage()

    def _config_for(
        self,
        served: list[_protocol.ObjectMetadata],
        params: Mapping[int, _protocol.ParamsEntry],
    ) -> dict[int, Mapping[str, Any]]:
        """The config columns one params table holds for the served rows."""
        return {
            meta.id: {"params": entry.params, "czas": entry.czas, "url": entry.url}
            for meta in served
            if (entry := params.get(meta.id)) is not None
        }

    def _apply_catalogue(
        self,
        served: list[_protocol.ObjectMetadata],
        config: Mapping[int, Mapping[str, Any]],
        applied: Applied,
    ) -> None:
        """Fold the whole object catalogue into the store, through the door.

        The door decides admission in one order on both tiers: the two
        presence rows go to their own types, a hidden row drops, and every
        remaining row must carry a leaf that parses. A row with an empty
        leaf is recorded on ``not_configured`` and left out, and
        :class:`NotConfigured` reports the set when it changes to a
        non-empty one. A leaf that does not parse is a server fault, raised
        here before any store field changes, so the reply is refused whole.
        """
        presence = [m for m in served if m.typ_komponentu in _protocol.PRESENCE_TYPES]
        rows = [m for m in served if m.typ_komponentu not in _protocol.PRESENCE_TYPES]
        admitted: list[tuple[_protocol.ObjectMetadata, ModuleAddress]] = []
        rejected: list[tuple[int, str | None]] = []
        for meta in rows:
            if config.get(meta.id, {}).get("params", 0) & HIDDEN_FLAG:
                continue
            if not meta.leaf_id:
                rejected.append((meta.id, meta.opis_menu))
                continue
            admitted.append((meta, _protocol.parse_module_address(meta.leaf_id)))
        # The presence merge parses the detection code before it mutates
        # anything, and nothing after it raises: the snapshot table holds
        # parsed seeds and a buffered push is already typed.
        self._merge_presence(presence, config, applied)
        touched = False
        for meta, address in admitted:
            touched |= self._merge_metadata(
                meta, address, config.get(meta.id, {}), applied
            )
        evicted = self._evict_missing_objects(
            {meta.id for meta, _ in admitted}, applied
        )
        if touched or evicted:
            self._rebuild_indexes(applied)
        self._set_not_configured(
            tuple(sorted(rejected, key=lambda pair: pair[0])), applied
        )

    def _set_not_configured(
        self, rejected: tuple[tuple[int, str | None], ...], applied: Applied
    ) -> None:
        """Record the rows the door left out and report a change to a non-empty set.

        ``rejected`` is sorted by id, so the order the reply listed the
        rows in never reads as a change.
        """
        if rejected == self.not_configured:
            return
        self.not_configured = rejected
        if rejected:
            applied.events.append(NotConfigured(objects=rejected))

    def _drop_sweep_entries(self, oid: int) -> None:
        """Forget what a sweep proved for one object.

        A sweep result belongs to an admitted object at one address, so an
        eviction and an address change both drop it until a later sweep
        covers the object again.
        """
        self._record_by_id.pop(oid, None)
        self._cover_parameters_by_id.pop(oid, None)
        self._lock_support_by_id.pop(oid, None)

    def _evict_missing_objects(self, present: set[int], applied: Applied) -> bool:
        """Drop the objects the door did not admit this time.

        The admitted set is the authority: a row the reply stopped listing,
        a row that now carries the hidden bit, and a row the door rejected
        all leave here, an empty reply included (a full grant revocation
        empties a restricted view). Each evicted id also drops its held
        sweep entries.
        """
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
            self._drop_sweep_entries(oid)
            applied.events.append(ObjectRemoved(obj))
        return True

    def _merge_presence(
        self,
        rows: list[_protocol.ObjectMetadata],
        config: Mapping[int, Mapping[str, Any]],
        applied: Applied,
    ) -> None:
        """Rebuild the two presence attributes from their catalogue rows.

        A row the reply stopped listing reads None, and so does a row that
        carries the hidden bit. The detection code is parsed into a local
        before any store field changes, so a malformed buffered push raises
        with the store still holding what it held before this call.
        """
        detection_meta = next(
            (m for m in rows if m.typ_komponentu == _protocol.DETECTION_TYPE), None
        )
        home_status = self._home_status
        if detection_meta is not None:
            pending = self._pending_state.get(detection_meta.id)
            if pending is not None:
                home_status = _home_status(pending.state)
            elif self._home_status is None:
                seed = self._stan_by_id.get(detection_meta.id)
                if seed is not None:
                    home_status = _home_status(seed.state)
        # No code below this line raises.
        previous_detection_id, previous_simulation_id = self._presence_ids()
        new_rows = {meta.id: meta for meta in rows}
        if previous_detection_id is not None and previous_detection_id not in new_rows:
            home_status = None
            self._detection_pushed = False
            self._stan_by_id.pop(previous_detection_id, None)
        if (
            previous_simulation_id is not None
            and previous_simulation_id not in new_rows
        ):
            self._stan_by_id.pop(previous_simulation_id, None)
        self._presence_rows = new_rows
        if detection_meta is not None:
            self._pending_state.pop(detection_meta.id, None)
        self._home_status = home_status
        detection: PresenceDetection | None = None
        simulation: PresenceSimulation | None = None
        for meta in rows:
            cfg = config.get(meta.id, {})
            if cfg.get("params", 0) & HIDDEN_FLAG:
                continue
            if meta.typ_komponentu == _protocol.DETECTION_TYPE:
                detection = PresenceDetection(
                    id=meta.id, name=meta.opis_menu, home_status=self._home_status
                )
            else:
                simulation = PresenceSimulation(
                    id=meta.id,
                    name=meta.opis_menu,
                    active=cfg.get("czas", 0) == 1,
                )
        self._set_presence(detection, simulation, applied)

    def _presence_ids(self) -> tuple[int | None, int | None]:
        """The current detection and simulation row ids, hidden ones included."""
        detection_id: int | None = None
        simulation_id: int | None = None
        for oid, meta in self._presence_rows.items():
            if meta.typ_komponentu == _protocol.DETECTION_TYPE:
                detection_id = oid
            else:
                simulation_id = oid
        return detection_id, simulation_id

    def _set_presence(
        self,
        detection: PresenceDetection | None,
        simulation: PresenceSimulation | None,
        applied: Applied,
    ) -> None:
        """Store both rows and report one event when either differs."""
        if (
            detection == self.presence_detection
            and simulation == self.presence_simulation
        ):
            return
        self.presence_detection = detection
        self.presence_simulation = simulation
        applied.events.append(
            PresenceChanged(detection=detection, simulation=simulation)
        )

    def _apply_presence_state(
        self, update: _protocol.StateUpdate, applied: Applied
    ) -> None:
        """Fold a push for one of the two rows.

        Only the detection row carries a value, the home-status code, held
        as store state whether or not the row is visible right now. A push
        for the simulation row changes nothing.
        """
        meta = self._presence_rows[update.id]
        if meta.typ_komponentu != _protocol.DETECTION_TYPE:
            return
        self._home_status = _home_status(update.state)
        self._detection_pushed = True
        detection = self.presence_detection
        if detection is not None:
            self._set_presence(
                replace(detection, home_status=self._home_status),
                self.presence_simulation,
                applied,
            )

    def _merge_metadata(
        self,
        meta: _protocol.ObjectMetadata,
        address: ModuleAddress,
        config: Mapping[str, Any],
        applied: Applied,
    ) -> bool:
        """Fold one catalogue row into its object; True when anything changed.

        Only a real change is reported, so re-requesting the catalogue on every
        reconnect does not hand a consumer a full set of updates that say
        nothing new.
        """
        obj = self.objects.get(meta.id)
        created = obj is None
        moved = False
        if obj is None:
            obj = AmpioObject(
                id=meta.id,
                typ_komponentu=meta.typ_komponentu,
                interpretacja=meta.interpretacja,
                funkcja=meta.funkcja,
                address=address,
                leaf_key=f"leaf_{meta.leaf_id}",
            )
        elif obj.address != address:
            # A leaf that moved the object to another channel invalidates
            # what a sweep proved for the old one.
            moved = True
            self._drop_sweep_entries(meta.id)
        updates: dict[str, Any] = {
            name: getattr(meta, name) for name in _METADATA_FIELDS
        }
        updates.update(config)
        updates["address"] = address
        updates["leaf_key"] = f"leaf_{meta.leaf_id}"
        if moved:
            # The raw form of the old channel says nothing about the new
            # one, so the object reads per-object reports until the new
            # channel reports.
            updates["raw_owned"] = False
            if _raw_channel_prefix(meta.typ_komponentu, address.sf_id) is not None:
                # The new leaf still bridges a raw channel, so the held
                # value is stale under a key `_rebuild_indexes` has not
                # rebuilt yet. It still carries the old channel's
                # local-clock stamp - not comparable to a server `on`
                # stamp - so the object stays local-stamped and only the
                # guard lifts: the very next report, live or snapshot,
                # replaces it outright rather than losing a stamp
                # comparison to a wall-clock value that was never a
                # server timestamp. A leaf that stopped bridging any
                # channel leaves the guard alone; `_rebuild_indexes`
                # already resets `raw_owned` for that case and the guard
                # waits for the next `begin_refresh` cycle, as it does for
                # any object that leaves the raw index.
                self._guarded.discard(meta.id)
        # The catalogue never carries a sweep result, so the held tables
        # re-apply on every merge, the re-creation after an eviction
        # included, and an entry dropped since the last merge clears.
        updates["record"] = self._record_by_id.get(meta.id)
        # A travel configuration and a lock answer belong to a roller kind
        # alone. A row that left the class drops both held entries, so a
        # later return to the class waits for a sweep of its own.
        if not _protocol.joins_roller_records(meta.typ_komponentu):
            self._cover_parameters_by_id.pop(meta.id, None)
            self._lock_support_by_id.pop(meta.id, None)
        updates["cover_parameters"] = self._cover_parameters_by_id.get(meta.id)
        updates["block_writable"] = self._lock_support_by_id.get(meta.id)
        changed = any(getattr(obj, name) != value for name, value in updates.items())
        updated = replace(obj, **updates)
        # The states snapshot is the one seed source on both tiers, so a
        # buffered snapshot value applies here and reply order never decides
        # whether an object starts with its state.
        seed = self._stan_by_id.get(meta.id)
        if seed is not None:
            updated, seeded = self._apply_stan_json(updated, seed)
            changed |= seeded
        # Replay a buffered push under the same stamp-supersedes rule the
        # seed follows: both carry the M-SERV's own clock.
        update = self._pending_state.pop(meta.id, None)
        if update is not None:
            stamp = float(update.on_ms) / 1000.0
            if self._supersedes(updated, stamp):
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

    def _handle_devices(self, data: Mapping[str, Any], applied: Applied) -> None:
        modules = _protocol.parse_devices(data)
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

    def _report_params_coverage(self) -> None:
        """Name the catalogue objects the params table carries no row for.

        The table covers the whole object catalogue, so every object the
        catalogue lists has a row on both tiers. A gap leaves those objects
        reading every Designer config flag as unset, which is a server
        fault to report rather than a state to model. Warned once per
        change, and held for diagnostics either way.
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

    def _handle_info(self, data: Mapping[str, Any], applied: Applied) -> None:
        """Apply a `data/info` reply, the M-SERV's self-report.

        The reply names the asking account, which is the wire's own verdict
        on the tier. The username decided the same question at
        construction, and every subscription and request follows from that
        decision, so a disagreement means the session is aimed at the wrong
        surfaces. Nothing in the reply can fix that, so it is refused.
        """
        info = _protocol.parse_server_info(data)
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

    def _handle_states_snapshot(
        self, data: Mapping[str, Any], applied: Applied
    ) -> None:
        """Apply a `data/states` reply, the one initial-value source.

        The snapshot answers both tiers, so neither catalogue needs to seed
        a value. Every row parses before any field changes, so a reply
        with one malformed row is refused whole and the held table stays.
        An id no catalogue established stays out of the store, and its
        seed waits here for the catalogue row that may establish it.
        """
        entries = _protocol.parse_states_snapshot(data)
        seeds = {
            entry.id: _protocol.parse_stan_json(entry.stan_json) for entry in entries
        }
        detection_id, _ = self._presence_ids()
        home_status = self._home_status
        if detection_id is not None and (
            self._home_status is None or not self._detection_pushed
        ):
            seed = seeds.get(detection_id)
            if seed is not None:
                home_status = _home_status(seed.state)
        # No code below this line raises.
        self._stan_by_id = seeds
        for oid, seed in seeds.items():
            obj = self.objects.get(oid)
            if obj is None:
                continue
            obj, changed = self._apply_stan_json(obj, seed)
            self.objects[oid] = obj
            if changed:
                self._record(obj, applied)
        self._home_status = home_status
        detection = self.presence_detection
        if detection is not None and detection.home_status != home_status:
            self._set_presence(
                replace(detection, home_status=home_status),
                self.presence_simulation,
                applied,
            )

    # --- live state -------------------------------------------------------

    def _apply_state(self, update: _protocol.StateUpdate, applied: Applied) -> None:
        if update.id in self._presence_rows:
            self._apply_presence_state(update, applied)
            return
        obj = self.objects.get(update.id)
        if obj is None:
            self._pending_state[update.id] = update
            return
        if obj.raw_owned:
            # The raw path owns this object: the per-object echo repeats
            # what the raw edge delivered ~150 ms earlier, so it is
            # dropped whole. It still counts as live evidence of the
            # module.
            self._touch_module(obj.address.mac)
            return
        obj = replace(
            obj,
            state=update.state,
            lammel=update.lammel if update.lammel is not None else obj.lammel,
            block=update.block if update.block is not None else obj.block,
            thermostat=(
                update.thermostat if update.thermostat is not None else obj.thermostat
            ),
            updated_at=float(update.on_ms) / 1000.0,
        )
        self.objects[update.id] = obj
        # The value now carries the M-SERV's own clock, so the local-stamp
        # bookkeeping a raw edge left behind no longer applies.
        self._local_stamped.discard(update.id)
        self._guarded.discard(update.id)
        self._touch_module(obj.address.mac)
        self._record(obj, applied)

    def _apply_raw_channel(
        self, edge: _protocol.RawChannelEdge, applied: Applied, *, retained: bool
    ) -> None:
        key = (edge.mac, edge.prefix, edge.channel)
        ids = self._input_index.get(key)
        if ids is None:
            # A replay waits for the routing table; a live frame for a
            # channel no object exposes is one nothing will ever route.
            if retained:
                self._pending_raw[key] = edge.state
            return
        # Two Designer views of one output share the module and the
        # channel, so one raw channel feeds every object on that channel.
        for oid in ids:
            obj = replace(
                self.objects[oid],
                raw_owned=True,
                state=edge.state,
                updated_at=time.time(),
            )
            self.objects[oid] = obj
            self._local_stamped.add(oid)
            self._guarded.add(oid)
            self._record(obj, applied)
        if not retained:
            self._touch_module(edge.mac)

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
            # The same rule as a raw channel edge: a replay waits for the
            # module list, a live frame for an unlisted module drops.
            if retained:
                self._pending_diagnostics[mac] = diagnostics
            return
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
        self, obj: AmpioObject, seed: _protocol.StanJsonSeed
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

    def module_by_mac(self, mac: int) -> AmpioModule | None:
        """The module row on ``mac``, or None when the list has none or two."""
        if mac in self.colliding_macs:
            return None
        mid = self._module_id_by_mac.get(mac)
        return None if mid is None else self.modules[mid]

    def _touch_module(self, mac: int) -> None:
        """Mark the module on ``mac`` as having produced live evidence just now.

        One clock only: the local receive time, because a live message is by
        definition received "now". Snapshot and catalogue seeds do not touch
        this - they replay DB state that may be arbitrarily old, which says
        nothing about whether the module is alive. A mac the module list
        does not carry touches nothing.
        """
        mid = self._module_id_by_mac.get(mac)
        if mid is not None:
            self.modules[mid] = replace(self.modules[mid], last_seen=time.time())

    def _rebuild_indexes(self, applied: Applied) -> None:
        """Rebuild the routing tables for the raw tree.

        The raw-channel index keys on the object's own `address.mac`, the
        override mac the leaf embeds, which the raw topics carry - never
        `mac_global`, which diverges from the raw-topic MAC on replaced
        modules. `(mac, prefix, channel)` routes a raw channel to every
        object on that channel: the bridgeable input types, plus
        `przekaznik` outputs on the `o` prefix, or on `a` for an
        open-collector leaf - a panel's status LEDs have no other retained
        surface, an OC output never echoes on its object topic, and every
        module's outputs share the channel shape. A `ledww` joins on the
        color-temperature prefix, whose channels its broadcast fans out to.
        `mac` alone routes a module's own diagnostics broadcast.
        """
        index: dict[tuple[int, str, int], tuple[int, ...]] = {}
        for obj in self.objects.values():
            prefix = _raw_channel_prefix(obj.typ_komponentu, obj.address.sf_id)
            if prefix is None:
                continue
            key = (obj.address.mac, prefix, obj.funkcja)
            index[key] = (*index.get(key, ()), obj.id)
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
        covered = {oid for ids in index.values() for oid in ids}
        for oid, obj in self.objects.items():
            if obj.raw_owned and oid not in covered:
                obj = replace(obj, raw_owned=False)
                self.objects[oid] = obj
                self._record(obj, applied)
        self._fold_pending_diagnostics(applied)
        self._fold_pending_raw(index, applied)

    def _fold_pending_raw(
        self, index: Mapping[tuple[int, str, int], tuple[int, ...]], applied: Applied
    ) -> None:
        """Apply the held channel values the fresh index can now route.

        Each lands exactly as the live replay of it would have: the value,
        the bridge claim on the object, and no touch of the module's
        `last_seen`, because a replay says what the channel last reported
        rather than that the module is alive now.
        """
        for key, state in list(self._pending_raw.items()):
            if key not in index:
                continue
            del self._pending_raw[key]
            mac, prefix, channel = key
            self._apply_raw_channel(
                _protocol.RawChannelEdge(
                    mac=mac, prefix=prefix, channel=channel, state=state
                ),
                applied,
                retained=True,
            )
        if index:
            # The routing table exists, so both catalogue replies have
            # landed, and whatever is still held is a channel no object
            # exposes. Holding it would let an arbitrarily old value reach
            # an object a later Designer save exposes.
            self._pending_raw.clear()

    def _fold_pending_diagnostics(self, applied: Applied) -> None:
        """Apply the held health frames for modules the list now carries.

        A replay says what a module last reported, not that it is alive now,
        so it leaves `last_seen` alone exactly as a live replay does (#174).
        """
        for mac, diagnostics in list(self._pending_diagnostics.items()):
            if mac not in self._module_id_by_mac:
                continue
            del self._pending_diagnostics[mac]
            self._apply_diagnostics(mac, diagnostics, applied, retained=True)

    def _record(self, obj: AmpioObject, applied: Applied) -> None:
        applied.events.append(ObjectUpdated(obj))


def _raw_channel_prefix(typ_komponentu: str, sf_id: int) -> str | None:
    """The raw-channel bridge prefix a `typ_komponentu`/leaf class pair
    reports on, or None when the pair bridges no channel.

    Mirrors the routing-index rule `_rebuild_indexes` builds the index by:
    the bridgeable input types, plus a `przekaznik` output on `o` or `a`
    for an open-collector leaf, plus a `ledww` on the color-temperature
    prefix. A merge that moves a leaf reads this before the index rebuilds,
    so it can tell whether the object stays raw-bridged under its new key.
    """
    prefix = input_channel_prefix(typ_komponentu)
    if prefix is None and typ_komponentu == "przekaznik":
        # A binary output reports on `o`; an open-collector output
        # (leaf class 67) reports a u8 on `a`, same 1-based channel.
        prefix = "a" if sf_id == _protocol.OC_OUTPUT_SF else "o"
    if prefix is None and typ_komponentu == "ledww":
        prefix = _protocol.CCT_PREFIX
    return prefix


def _home_status(state: str) -> int:
    """The detection row's home-status code, an integer on the wire."""
    try:
        return int(state)
    except ValueError as err:
        raise AmpioProtocolError(
            f"The Ampio presence-detection state {state!r} is not a home-status code"
        ) from err


# The object fields both catalogue surfaces own, derived from the shared
# row's own shape so a new column is added in one place and flows through
# the merge. `id` keys the merge, so it is not metadata. `leaf_id` is the
# door's input: it reaches the object as `address` and `leaf_key`, not as
# a field of its own. The Designer config columns are not here: each tier
# serves them from its own surface, and the merge takes them as `config`.
_METADATA_FIELDS = tuple(
    f.name for f in fields(_protocol.ObjectMetadata) if f.name not in ("id", "leaf_id")
)
