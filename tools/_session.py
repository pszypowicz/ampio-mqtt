"""The client class a tool builds from the username it was given."""

from __future__ import annotations

from collections.abc import Callable

import aiomqtt

from ampio_mqtt import AccessTier, AmpioAdminClient, AmpioClient

# The reserved login name, read from the one place the library writes it.
ADMIN_USERNAME = AccessTier.ADMIN.value


def make_client(
    host: str,
    username: str,
    password: str | None,
    *,
    port: int,
    client_factory: Callable[[], aiomqtt.Client] | None,
) -> AmpioClient:
    """The admin client for the reserved login, the base client otherwise."""
    if username == ADMIN_USERNAME:
        return AmpioAdminClient(
            host, password, port=port, mqtt_client_factory=client_factory
        )
    return AmpioClient(
        host, username, password, port=port, mqtt_client_factory=client_factory
    )
