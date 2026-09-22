"""A seam for consumer test fixtures.

A fixture that seeds model instances by hand can hold a row the wire
cannot produce. This module drives a decoded reply through the store the
client itself builds, so a fixture carries what the admission door
admits and nothing else.

Two functions and five store read attributes are the promise here:
``objects``, ``not_configured`` and ``server_info`` on
:class:`AmpioStore`, and ``modules`` and ``collisions`` on
:class:`AdminStore`. ``objects``, ``server_info`` and ``modules`` carry
what the matching :class:`~ampio_mqtt.AmpioClient` attribute carries. The
client reports the two refusal sets through ``diagnostics_snapshot()``
instead, under ``not_configured`` and ``mac_collisions``.

Apply a reply with :func:`apply_reply` rather than through the store,
because the store serves the library and its signature follows the
library's needs. Nothing else on either class is promised.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, overload

from ._protocol import ENDPOINT_BY_NAME
from ._store import AdminStore, AmpioStore
from .client import AmpioAdminClient, AmpioClient
from .events import StoreEvent

__all__ = ["AdminStore", "AmpioStore", "apply_reply", "build_store"]


@overload
def build_store(client: type[AmpioAdminClient]) -> AdminStore: ...


@overload
def build_store(client: type[AmpioClient]) -> AmpioStore: ...


def build_store(client: type[AmpioClient]) -> AmpioStore:
    """The store that client class builds.

    The client class is the account tier, so a fixture names the class it
    uses in production and is served exactly what that session holds.
    """
    return client._store_class()


def apply_reply(
    store: AmpioStore, endpoint: str, payload: Mapping[str, Any]
) -> list[StoreEvent]:
    """Apply one decoded table reply and report the events it produced.

    ``endpoint`` names the reply the way the endpoint table does, for
    example ``data_devices``, ``params_devices`` or ``devices``.
    ``payload`` is the decoded envelope, not the reply bytes.

    A name the library does not serve raises ``KeyError``. A name it
    serves that this store applies no reply for raises ``RuntimeError``.
    ``devices`` on a base store is that case, and so is any endpoint
    whose reply the client parses instead of the store.
    """
    return store.apply_endpoint(ENDPOINT_BY_NAME[endpoint], payload).events
