"""Everything the library knows, and how an inbound message changes it.

Pure state: no sockets, no tasks, no listeners. `apply()` and
`apply_endpoint()` take one routed message or decoded table reply and report
what it touched, so the caller decides who to tell. That
also makes every protocol behavior here reachable from a plain function call.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import Any

from . import _protocol
from .classification import input_channel_prefix
from .errors import AmpioNotConfigured
from .events import (
    BusEventRaised,
    ModuleRemoved,
    ModuleUpdated,
    NotConfigured,
    ObjectAdded,
    ObjectRemoved,
    ObjectUpdated,
    StoreEvent,
)
from .models import (
    HIDDEN_FLAG,
    AmpioModule,
    AmpioObject,
    AmpioServerInfo,
    CoverParameters,
    DesignerRecord,
    ModuleAddress,
    ModuleRecord,
    PanelSettings,
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
    """Applies typed M-SERV messages to the object and server state.

    Holds what every account is served: the object catalogue, its config
    columns, the states snapshot and the server's self-report.
    :class:`AdminStore` adds what the reserved admin login alone receives
    (docs/account-tiers.md). Each fact has one source: `data/params_devices`
    carries the Designer config columns for every account.
    """

    def __init__(self) -> None:
        self.objects: dict[int, AmpioObject] = {}
        self.server_info: AmpioServerInfo | None = None
        # The served rows of the last `data/devices` reply, system rows
        # dropped, held so a `data/params_devices` push alone re-runs the
        # door on them. None until the first reply.
        self._catalogue: list[_protocol.ObjectMetadata] | None = None
        # The `(id, name)` pairs of the rows the door left out because they
        # carry no leaf, from the last apply. Empty when every listed row
        # was admitted or hidden. The client raises and reports from it.
        self.not_configured: tuple[tuple[int, str | None], ...] = ()
        # Full-catalogue per-object config facts (`params`, `czas`, `url`)
        # from `data/params_devices`, this store's one source for them on
        # both tiers. Held because the two catalogue replies arrive in no
        # fixed order, and re-applied on every merge - an eviction included.
        self._params_by_id: dict[int, _protocol.ParamsEntry] = {}
        # Whether the config table has answered at least once, so a gap in
        # its coverage is told apart from a table still in flight.
        self._params_received = False
        # Admitted objects the params table carries no row for. Every
        # object the catalogue lists has a row on both tiers, so a
        # non-empty set is a server fault: those objects read every
        # Designer config flag as unset. Warned once per change and
        # surfaced for diagnostics.
        self.missing_params_ids: frozenset[int] = frozenset()
        # `{object_id: seed}` from the last `data/states` snapshot, held
        # because the snapshot and the catalogue replies arrive in no fixed
        # order; a snapshot row for an id no catalogue established creates
        # nothing.
        self._stan_by_id: dict[int, _protocol.StanJsonSeed] = {}
        # Ids whose held seed came from a snapshot before the latest request.
        # Such a seed still starts an object that holds no value yet, but it
        # never replaces a value an existing object holds.
        self._previous_seeds: set[int] = set()
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
        self._handlers = self._handler_table()
        # The endpoint table and the handler table are edited separately;
        # a name typo between them would otherwise surface as a silent
        # discovery hang, so misalignment fails construction instead.
        handler_gated = {ep.name for ep in self._endpoint_set() if ep.parses is None}
        if set(self._handlers) != handler_gated:
            raise RuntimeError(
                f"store handlers {sorted(self._handlers)} do not match the "
                f"handler-gated endpoints {sorted(handler_gated)}"
            )

    def _endpoint_set(self) -> tuple[_protocol.Endpoint, ...]:
        """The endpoints this store's handlers must cover."""
        return _protocol.BASE_ENDPOINTS

    def _handler_table(self) -> dict[str, Callable[[Mapping[str, Any], Applied], None]]:
        """The reply handlers this store applies, by endpoint name.

        Every endpoint here answers with a reply that mutates state. The
        rest are pure request/response, parsed by the dispatcher with the
        endpoint's own `parses` gate and never sent to a store.
        """
        return {
            "states": self._handle_states_snapshot,
            "info": self._handle_info,
            "data_devices": self._handle_catalogue,
            "params_devices": self._handle_params_devices,
        }

    # --- routing ----------------------------------------------------------

    def begin_refresh(self) -> None:
        """Mark the start of a snapshot request cycle.

        Every value held now predates the snapshot the new cycle will
        deliver, so a dated seed may correct locally-stamped values again.
        The seeds of the previous snapshot stay for an object the catalogue
        adds later, and they no longer replace a value an existing object
        holds. The client calls this before it publishes the discovery
        requests.
        """
        self._guarded.clear()
        self._previous_seeds = set(self._stan_by_id)

    def apply(self, msg: _protocol.Inbound, *, retained: bool = False) -> Applied:
        """Apply one typed message and report what it changed.

        The base store applies the account namespace alone: a per-object
        push. The base router routes no shape but the endpoint replies and
        the per-object push, so any other message here is an invariant
        broken. ``retained`` marks a broker replay, which the admin store
        reads.
        """
        applied = Applied()
        if isinstance(msg, _protocol.StateUpdate):
            self._apply_state(msg, applied)
            return applied
        raise RuntimeError(
            f"{type(msg).__name__} is not a message the base store applies"
        )

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
            # The client sends only handler-gated served endpoints here.
            # `testing.apply_reply` can send any served endpoint, and this
            # raise is its documented refusal.
            raise RuntimeError(
                f"endpoint {endpoint.name!r} has no handler on this store"
            )
        handler(data, applied)
        return applied

    # --- admin hooks ------------------------------------------------------

    def _touch_module(self, mac: int) -> None:
        """Mark the module on ``mac`` as alive now. The base holds no module list."""

    def _rebuild_indexes(self, applied: Applied) -> None:
        """Rebuild the raw routing after a catalogue change.

        The base routes no raw tree.
        """

    def _raw_owns(self, oid: int) -> bool:
        """Whether the raw path owns the object. The base has no raw path."""
        return False

    def _release_raw(self, oid: int) -> None:
        """Return the object to per-object updates. The base has nothing to release."""

    # --- catalogues -------------------------------------------------------

    def _handle_catalogue(self, data: Mapping[str, Any], applied: Applied) -> None:
        """Apply a `data/devices` reply through the door, then hold it.

        The reply carries two rows the M-SERV creates itself, and no module
        drives either one. The handler drops them by type, so the held
        reply, the door and the buffered-push prune never see one.

        The door runs once the params table is in hand, and a reply the
        door refuses leaves every held field as it was. A reply that
        arrives before the params table is held unseen, and a later params
        push runs the door on it.
        """
        served = [
            meta
            for meta in _protocol.parse_app_sync_devices(data)
            if meta.typ_komponentu not in _protocol.SYSTEM_ROW_TYPES
        ]
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
        push of the table alone re-runs the door on the held catalogue, so
        a hidden bit that changes evicts or admits its row. A table the
        door refuses is not held.
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

        The door decides admission in one order on both tiers: a hidden row
        drops before its leaf is read because nothing drives it, and every
        remaining row must carry a leaf that parses. A row with an empty
        leaf is recorded on ``not_configured`` and left out, and
        :class:`NotConfigured` reports every change of the set, a change
        to empty included. A leaf that does not parse is a server fault, raised
        here before any store field changes, so the reply is refused whole.
        """
        admitted: list[tuple[_protocol.ObjectMetadata, ModuleAddress]] = []
        rejected: list[tuple[int, str | None]] = []
        for meta in served:
            if config.get(meta.id, {}).get("params", 0) & HIDDEN_FLAG:
                continue
            if not meta.leaf_id:
                rejected.append((meta.id, meta.name))
                continue
            admitted.append((meta, _protocol.parse_module_address(meta.leaf_id)))
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
        """Record the rows the door left out and report any change of the set.

        ``rejected`` is sorted by id, so the order the reply listed the
        rows in never reads as a change.
        """
        if rejected == self.not_configured:
            return
        self.not_configured = rejected
        applied.events.append(self._admission())

    def _admission(self) -> NotConfigured:
        """Everything the door refuses now, both sides.

        The event and the error read this one answer, so a consumer that
        listens and a consumer that polls are never told different things.
        The base store fills the object side alone, because a standard
        account is served no module list.
        """
        return NotConfigured(objects=self.not_configured)

    def admission_failure(self) -> AmpioNotConfigured | None:
        """The installer fault the last replies left, or None."""
        state = self._admission()
        if not state.objects and not state.collisions:
            return None
        return AmpioNotConfigured(objects=state.objects, collisions=state.collisions)

    def _drop_sweep_entries(self, oid: int) -> None:
        """Forget what a sweep proved for one object. The base holds no sweep."""

    def _drop_cover_parameters(self, oid: int) -> None:
        """Forget one object's travel parameters. The base holds no sweep."""

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
            self._release_raw(oid)
            self._drop_sweep_entries(oid)
            applied.events.append(ObjectRemoved(obj))
        return True

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
        leaf_key = f"leaf_{meta.leaf_id}"
        if obj is None:
            obj = AmpioObject(
                id=meta.id,
                typ_komponentu=meta.typ_komponentu,
                interpretacja=meta.interpretacja,
                funkcja=meta.funkcja,
                address=address,
                leaf_key=leaf_key,
            )
        elif obj.address != address:
            # A leaf that moved the object to another channel invalidates
            # what a sweep proved for the old one.
            moved = True
            self._drop_sweep_entries(meta.id)
        kind = meta.typ_komponentu
        if obj.typ_komponentu != kind and not _protocol.joins_roller_records(kind):
            # Travel parameters belong to a roller channel, and a kind
            # outside the roller class joins none. A fresh object carries
            # the row's own kind, so only a retype reaches here.
            self._drop_cover_parameters(meta.id)
        updates: dict[str, Any] = {
            name: getattr(meta, name) for name in _METADATA_FIELDS
        }
        updates.update(config)
        updates["address"] = address
        updates["leaf_key"] = leaf_key
        if moved:
            # The raw form of the old channel says nothing about the new
            # one, so the object reads per-object reports until the new
            # channel reports, and the guard lifts because the held value
            # belongs to another channel.
            self._release_raw(meta.id)
        changed = any(getattr(obj, name) != value for name, value in updates.items())
        updated = replace(obj, **updates)
        # The states snapshot is the one seed source on both tiers, so a
        # buffered snapshot value applies here and reply order never decides
        # whether an object starts with its state.
        seed = self._stan_by_id.get(meta.id)
        if seed is not None and (created or meta.id not in self._previous_seeds):
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

    def _report_params_coverage(self) -> None:
        """Name the admitted objects the params table carries no row for.

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

        The reply names the asking account, a wire fact the store records
        as it reads it.
        """
        info = _protocol.parse_server_info(data)
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
        # No code below this line raises.
        self._stan_by_id = seeds
        self._previous_seeds.clear()
        for oid, seed in seeds.items():
            obj = self.objects.get(oid)
            if obj is None:
                continue
            obj, changed = self._apply_stan_json(obj, seed)
            self.objects[oid] = obj
            if changed:
                self._record(obj, applied)

    # --- live state -------------------------------------------------------

    def _apply_state(self, update: _protocol.StateUpdate, applied: Applied) -> None:
        obj = self.objects.get(update.id)
        if obj is None:
            self._pending_state[update.id] = update
            return
        if self._raw_owns(update.id):
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
        if self._raw_owns(obj.id):
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
        """Whether a report stamped by the M-SERV should replace what `obj` holds.

        A snapshot row and a buffered live push each carry the M-SERV stamp
        they were reported at, so
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

    def _record(self, obj: AmpioObject, applied: Applied) -> None:
        applied.events.append(ObjectUpdated(obj))


class AdminStore(AmpioStore):
    """The store for the reserved admin login.

    Adds what the M-SERV serves that account alone: the module list, the
    raw tree with its routing index and raw ownership, the module
    diagnostics broadcasts, and the description-record sweep datasets.
    """

    def __init__(self) -> None:
        super().__init__()
        self.modules: dict[int, AmpioModule] = {}
        # The override macs two or more module rows share, with those
        # rows' ids, from the last module list.
        self.collisions: tuple[tuple[int, tuple[int, ...]], ...] = ()
        # Raw-channel bridge: (module mac, prefix, channel) -> the ids of
        # every object on that channel.
        self._input_index: dict[tuple[int, str, int], tuple[int, ...]] = {}
        # Effective bus mac -> module id, for routing a module's own
        # broadcasts. Ids, not instances: modules are frozen and replaced on
        # every change, so a cached instance would go stale.
        self._module_id_by_mac: dict[int, int] = {}
        # The ids the raw path owns: a per-object echo and a snapshot row
        # for one of them is skipped, because the retained raw tree is its
        # resync. docs/raw-channel-bridge.md.
        self._raw_owned: set[int] = set()
        # The broker replays its retained raw tree within a second of the
        # subscribe, before any catalogue reply, so the routing tables above
        # are still empty when those frames land. They wait here, keyed the
        # way the tables key them, and `_rebuild_indexes` folds them in once
        # the catalogue builds the routing.
        # Each held value keeps the local time it arrived.
        self._pending_raw: dict[tuple[int, str, int], tuple[str, float]] = {}
        self._pending_diagnostics: dict[int, _protocol.ModuleDiagnostics] = {}
        # The sweep datasets, by object id and by mac.
        # docs/description-records.md.
        self.records: dict[int, DesignerRecord] = {}
        self.cover_parameters: dict[int, CoverParameters] = {}
        self.module_records: dict[int, ModuleRecord] = {}
        self.capabilities: dict[int, Mapping[int, int]] = {}
        self.panel_settings: dict[int, PanelSettings] = {}

    def _endpoint_set(self) -> tuple[_protocol.Endpoint, ...]:
        return _protocol.ADMIN_ENDPOINTS

    def _handler_table(self) -> dict[str, Callable[[Mapping[str, Any], Applied], None]]:
        return {**super()._handler_table(), "devices": self._handle_devices}

    # --- routing ----------------------------------------------------------

    def apply(self, msg: _protocol.Inbound, *, retained: bool = False) -> Applied:
        """Apply one typed message and report what it changed.

        Adds the shapes the admin session receives: the raw tree and the
        module broadcasts. ``retained`` marks a broker replay from its
        retained store. A replay carries the value but says nothing about
        whether the module is alive now, so it never touches ``last_seen``.
        """
        applied = Applied()
        match msg:
            case _protocol.RawChannelEdge() as edge:
                self._apply_raw_channel(edge, applied, retained=retained)
            case _protocol.ColorTempFrame(edges=edges):
                for edge in edges:
                    self._apply_raw_channel(edge, applied, retained=retained)
            case _protocol.DiagnosticsReport(mac=mac, diagnostics=diagnostics):
                self._apply_diagnostics(mac, diagnostics, applied, retained=retained)
            case BusEventRaised() as event:
                applied.events.append(event)
            case _:
                return super().apply(msg, retained=retained)
        return applied

    def apply_sweep(
        self,
        answered_macs: frozenset[int],
        records: Mapping[int, DesignerRecord],
        cover_parameters: Mapping[int, CoverParameters],
        module_records: Mapping[int, ModuleRecord],
        capabilities: Mapping[int, Mapping[int, int]],
        panel_settings: Mapping[int, PanelSettings],
    ) -> None:
        """Replace the datasets of every module that answered one sweep.

        An entry an answered module no longer carries leaves, so absence
        with the mac answered is authoritative until the next sweep. A
        module the sweep did not answer keeps its entries. Each capability
        map is held read-only, because the client hands it out as it is.
        No model field changes, so nothing is reported.
        """
        for oid, obj in self.objects.items():
            if obj.address.mac in answered_macs:
                self.records.pop(oid, None)
                self.cover_parameters.pop(oid, None)
        for mac in answered_macs:
            self.module_records.pop(mac, None)
            self.capabilities.pop(mac, None)
            self.panel_settings.pop(mac, None)
        self.records.update(records)
        self.cover_parameters.update(cover_parameters)
        self.module_records.update(module_records)
        self.capabilities.update(
            (mac, MappingProxyType(dict(functions)))
            for mac, functions in capabilities.items()
        )
        self.panel_settings.update(panel_settings)

    def _drop_sweep_entries(self, oid: int) -> None:
        self.records.pop(oid, None)
        self.cover_parameters.pop(oid, None)

    def _drop_cover_parameters(self, oid: int) -> None:
        self.cover_parameters.pop(oid, None)

    def _drop_module_datasets(self, mac: int) -> None:
        """Forget what a sweep proved for the module on ``mac``.

        The mac-keyed entries belong to the module row that carries the
        mac, so a row that leaves the list takes them with it.
        """
        self.module_records.pop(mac, None)
        self.capabilities.pop(mac, None)
        self.panel_settings.pop(mac, None)

    # --- admin hooks ------------------------------------------------------

    def _raw_owns(self, oid: int) -> bool:
        return oid in self._raw_owned

    def _release_raw(self, oid: int) -> None:
        self._raw_owned.discard(oid)
        self._guarded.discard(oid)

    # --- catalogues -------------------------------------------------------

    def _handle_devices(self, data: Mapping[str, Any], applied: Applied) -> None:
        """Fold the module list into the store, through the door.

        The list is admin-only and complete, so its arrival is the
        authority to evict what it stopped listing or stopped admitting.
        A mac two rows share admits neither: the raw tree keys on that
        mac and cannot attribute a frame to either row. Those rows are
        recorded on ``collisions``, and :class:`NotConfigured` reports
        every change of the set, a change to empty included.
        """
        modules = _protocol.parse_devices(data)
        rows_by_mac: dict[int, list[AmpioModule]] = {}
        for module in modules:
            rows_by_mac.setdefault(module.mac, []).append(module)
        collisions = tuple(
            (mac, tuple(sorted(row.id for row in rows)))
            for mac, rows in sorted(rows_by_mac.items())
            if len(rows) > 1
        )
        admitted = [module for module in modules if len(rows_by_mac[module.mac]) == 1]
        changed = False
        for module in admitted:
            previous = self.modules.get(module.id)
            if previous is not None:
                module = replace(
                    module,
                    last_seen=previous.last_seen,
                    supply_voltage=previous.supply_voltage,
                    temperature=previous.temperature,
                )
            self.modules[module.id] = module
            # A new module or a changed catalogue row is news, exactly as an
            # object catalogue row is; the live fields were carried over
            # above, so any difference left is the catalogue's.
            if previous != module:
                changed = True
                applied.events.append(ModuleUpdated(module))
        present = {module.id for module in admitted}
        missing = [mid for mid in self.modules if mid not in present]
        evicted = False
        for mid in missing:
            evicted = True
            module = self.modules.pop(mid)
            self._drop_module_datasets(module.mac)
            applied.events.append(ModuleRemoved(module))
        if changed or evicted:
            self._rebuild_indexes(applied)
        self._set_collisions(collisions, applied)

    def _set_collisions(
        self, collisions: tuple[tuple[int, tuple[int, ...]], ...], applied: Applied
    ) -> None:
        """Record the macs the door refused and report any change of the set.

        Both the macs and the ids on each are sorted, so the order the
        reply listed the rows in never reads as a change.
        """
        if collisions == self.collisions:
            return
        self.collisions = collisions
        applied.events.append(self._admission())

    # --- live state -------------------------------------------------------

    def _apply_raw_channel(
        self,
        edge: _protocol.RawChannelEdge,
        applied: Applied,
        *,
        retained: bool,
        received_at: float | None = None,
    ) -> None:
        key = (edge.mac, edge.prefix, edge.channel)
        ids = self._input_index.get(key)
        if ids is None:
            # A replay waits for the routing table. A live frame with no
            # route replaces a value held for its channel, so a held value
            # is never older than the channel's latest frame. With nothing
            # held, the live frame drops.
            if retained or key in self._pending_raw:
                self._pending_raw[key] = (edge.state, time.time())
            return
        # Two Designer views of one output share the module and the
        # channel, so one raw channel feeds every object on that channel.
        for oid in ids:
            self._raw_owned.add(oid)
            stamp = time.time() if received_at is None else received_at
            obj = replace(self.objects[oid], state=edge.state, updated_at=stamp)
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
            # A replay waits for the module list, and a live frame for an
            # unlisted module drops.
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

    def _admission(self) -> NotConfigured:
        """Both sides: the admin session is served the module list too."""
        return NotConfigured(objects=self.not_configured, collisions=self.collisions)

    def module_by_mac(self, mac: int) -> AmpioModule | None:
        """The module row on ``mac``, or None when the list has none."""
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
        """Rebuild the raw routing: `(address.mac, prefix, funkcja)` to the
        objects on that channel, and `mac` to the module id
        (docs/raw-channel-bridge.md).
        """
        index: dict[tuple[int, str, int], tuple[int, ...]] = {}
        for obj in self.objects.values():
            prefix = input_channel_prefix(obj.typ_komponentu)
            if prefix is None and obj.typ_komponentu == "przekaznik":
                prefix = "a" if obj.address.sf_id == _protocol.OC_OUTPUT_SF else "o"
            if prefix is None and obj.typ_komponentu == "ledww":
                prefix = _protocol.CCT_PREFIX
            if prefix is None:
                continue
            key = (obj.address.mac, prefix, obj.funkcja)
            index[key] = (*index.get(key, ()), obj.id)
        self._input_index = index
        self._module_id_by_mac = {
            module.mac: module.id for module in self.modules.values()
        }
        # An object the index no longer covers (a retype out of a bridged
        # kind) goes back to its per-object updates. Ownership is store
        # bookkeeping, so the release changes nothing a consumer can read
        # and reports nothing.
        covered = {oid for ids in index.values() for oid in ids}
        self._raw_owned.intersection_update(covered)
        # The guard stays set on an object the index stopped covering,
        # because its held value still belongs to its own channel and the
        # next request cycle lifts it, while a moved leaf lifts the guard
        # through `_release_raw`.
        self._fold_pending_diagnostics(applied)
        self._fold_pending_raw(index, applied)

    def _fold_pending_raw(
        self, index: Mapping[tuple[int, str, int], tuple[int, ...]], applied: Applied
    ) -> None:
        """Apply the held channel values the fresh index can now route.

        Each lands with the time it arrived and the raw ownership in the store's set.
        It does not touch the module's `last_seen`, because a held value says
        what the channel reported rather than that the module is alive now.
        """
        for key, (state, received_at) in list(self._pending_raw.items()):
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
                received_at=received_at,
            )
        if index:
            # A rebuild drops what the fresh index still does not route. A
            # replay that lands after this rebuild waits for the next one,
            # and a live frame keeps it current until then.
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


# The object fields the `data/devices` catalogue owns, derived from the
# shared row's own shape so a new column is added in one place and flows
# through the merge. `id` keys the merge, so it is not metadata. `leaf_id`
# is the door's input: it reaches the object as `address` and `leaf_key`,
# not as a field of its own. The Designer config columns come from
# `data/params_devices`, and the merge takes them as `config`.
_METADATA_FIELDS = tuple(
    f.name for f in fields(_protocol.ObjectMetadata) if f.name not in ("id", "leaf_id")
)
