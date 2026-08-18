"""Endpoint table, topics, and command payloads for the Ampio MQTT protocol.

Protocol: MQTT topics are namespaced by the connecting account:
  state:     ampio/fromDB/<user>/ob/<id>/state   -> {"state","desc","on"}
  objects:   publish ampio/control/<user>/config = "devicesDetails"
             -> ampio/fromDB/<user>/config/devicesDetails = {"Status":0,"List":[...]}
  modules:   publish ampio/control/<user>/config = "devices"
             -> ampio/fromDB/<user>/config/devices = {"List":[{id,mac,
                nazwa_urzadzenia,typ_urzadzenia,wersja_softu,...}]}

The same ampio/control/<user>/config topic carries every discovery request; the
payload keyword selects what the server publishes back. The `config` surface
answers only for administrator accounts. Non-admin accounts are served the
app-sync `data` surface instead: `data/devices` (objects, grant-filtered to
what the account can see in the app; same row shape as `devicesDetails` minus
`params`/`stan_json`) and `data/params_devices` (the `params` bitfields,
unfiltered). See `AccessTier` and the discovery groups below.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

# --- Endpoint table --------------------------------------------------------
#
# Every request/response endpoint the M-SERV exposes is one row here, and that
# row is the single source of truth: the client derives its subscriptions,
# topic-to-handler routing, discovery-completion signals, and retained payloads
# from this table. Adding an endpoint is one row, not edits in four places.
#
# A request publishes ``req_payload`` (a keyword, or "" for the dedicated
# ``states``/``info`` surfaces) to ``ampio/control/<user>/<req_surface>``; the
# reply lands on ``ampio/fromDB/<user>/<resp_surface>/<resp_leaf>``.


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One M-SERV request/response endpoint."""

    name: str
    req_surface: str  # control sub-topic: "config" | "states" | "info" | "data"
    req_payload: str  # request keyword, or "" for the states/info surfaces
    resp_surface: str  # fromDB sub-topic: "config" | "data"
    resp_leaf: str  # final response-topic segment
    # Part of the initial-discovery set awaited by start() /
    # wait_for_initial_discovery(). The rooms/scenes endpoints are on-demand.
    initial: bool = False


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("details", "config", "devicesDetails", "config", "devicesDetails", True),
    Endpoint("devices", "config", "devices", "config", "devices", True),
    Endpoint("states", "states", "", "data", "states", True),
    Endpoint("info", "info", "", "data", "info", True),
    # App-sync object catalogue. Same wire keyword as the module list above but
    # on the `data` surface, and a different payload: DB objects (the
    # `devicesDetails` row shape minus `params`/`stan_json`), filtered to the
    # objects the account was granted in the Ampio app. Unlike the `config`
    # surface it answers for every account, so it is the discovery fallback
    # for non-admin accounts.
    Endpoint("data_devices", "data", "devices", "data", "devices", True),
    # Per-object `params` bitfields for the app-sync catalogue. NOT
    # grant-filtered: every account receives the full table, which is what
    # lets a restricted account apply the hidden-flag visibility rule.
    Endpoint(
        "params_devices", "data", "params_devices", "data", "params_devices", True
    ),
    Endpoint("groups", "data", "groups", "data", "groups"),
    Endpoint("group_devices", "data", "group_devices", "data", "group_devices"),
    Endpoint("scenes", "data", "scenes", "data", "scenes"),
)

ENDPOINT_BY_NAME: dict[str, Endpoint] = {ep.name: ep for ep in ENDPOINTS}


# The M-SERV software baseline this library is developed and live-tested
# against, as the server self-reports it on the info surface. This is the
# compatibility floor, not a promise about anything older: a lower (or
# missing) serverVersion logs a warning at discovery and behavior on such a
# server is undefined - the fix is upgrading the M-SERV. The baseline server
# also reported serverRevision 409 and mqttVersion 5.133.11, recorded in the
# README; only serverVersion is compared.
BASELINE_SERVER_VERSION = (1865,)


class AccessTier(Enum):
    """Account tier, derived from the account id in the server-info reply.

    The M-SERV gates the ``config`` surface (and the raw ``ampio/from/#``
    channel tree) on the account being the reserved ``admin`` login; the
    per-user app permissions do not affect it. A non-admin account, however
    permissioned, is served only the app-sync ``data`` surface.
    """

    UNKNOWN = "unknown"  # no info reply yet (a baseline server always ids it)
    ADMIN = "admin"  # the reserved `admin` login: full catalogue + modules
    RESTRICTED = "restricted"  # an app-created user: app-sync view only


# Initial-discovery endpoint groups by tier. Discovery is complete when the
# common pair plus the catalogue pair of the account's tier have latched;
# the tier is read from the info reply (see `AmpioServerInfo.access_tier`).
DISCOVERY_COMMON: tuple[str, ...] = ("states", "info")
DISCOVERY_ADMIN: tuple[str, ...] = ("details", "devices")
DISCOVERY_FALLBACK: tuple[str, ...] = ("data_devices", "params_devices")


# --- Commands --------------------------------------------------------------
#
# Writes go to one control topic per account as plain text:
# ``/api/set/<object_id>/<verb>[/<arg>...]``. The verb vocabulary is the
# M-SERV's own HTTP API, re-exposed over MQTT; see docs/protocol.md for
# the verb table.
#
# The per-user grant bounds writes as it bounds reads: a command for an object
# outside the account's grant is dropped with no effect and no reply.


def command_topic(user: str) -> str:
    """Control topic that carries object commands for an account."""
    return f"ampio/control/{user}/api"


def command_payload(object_id: int, verb: str, args: Sequence[object] = ()) -> str:
    """Build an ``/api/set`` command payload."""
    return f"/api/set/{object_id}/{verb}" + "".join(f"/{a}" for a in args)


def event_payload(event_number: int) -> str:
    """Build the payload that raises a bus event."""
    return f"/api/setEvent/{event_number}"


def scene_payload(scene_id: int, verb: str) -> str:
    """Build a scene command payload; ``verb`` is run, off, or undo."""
    return f"/api/{verb}/scene/{scene_id}"


# `setRollerPos` takes a position and a lamella angle. 101 on either axis means
# "leave this one where it is", so one command can move either axis alone or
# both together.
KEEP_POSITION = 101


def request_topic(ep: Endpoint, user: str) -> str:
    """Control topic an endpoint's request keyword is published to."""
    return f"ampio/control/{user}/{ep.req_surface}"


def response_topic(ep: Endpoint, user: str) -> str:
    """fromDB topic an endpoint's reply arrives on."""
    return f"ampio/fromDB/{user}/{ep.resp_surface}/{ep.resp_leaf}"


def ob_state_wildcard(user: str) -> str:
    """Wildcard for all object state topics for an account."""
    return f"ampio/fromDB/{user}/ob/+/state"


# Raw, module-scoped channel topics carry decoded CAN state per channel index
# and are NOT namespaced by user (the `ampio/from/<MAC>/...` tree is global).
# We subscribe only to the two input prefixes - `f` (flags) and `i` (digital
# inputs) - because they publish on-change and are the low-latency source for
# input objects. The high-rate prefixes (`a`/`t`/`rgbw`/`o`) are intentionally
# excluded; those object types already arrive on the per-object topic.
RAW_INPUT_WILDCARDS = ("ampio/from/+/state/f/+", "ampio/from/+/state/i/+")

# Modules periodically broadcast a diagnostics frame on `ampio/from/<MAC>/b/4F`
# carrying their CAN supply voltage and, on the modules that measure it, their
# own temperature. Like the rest of the raw tree this is administrator-only.
RAW_DIAGNOSTICS_WILDCARD = "ampio/from/+/b/4F"

# Bus events are logical signals (1-65535) that Ampio logic raises and reacts
# to - a wall-panel press can raise one, and a scenario can be bound to one.
# Receiving them rides the administrator-only raw tree. Raising one goes to the
# command surface, works on both tiers, and is bounded by nothing - not object
# grants, and not the per-event rights the app displays.
RAW_EVENT_WILDCARD = "ampio/from/+/event"
