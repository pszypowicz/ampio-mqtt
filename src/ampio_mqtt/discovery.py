"""Best-effort discovery of an Ampio M-SERV broker on the LAN."""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo

_LOGGER = logging.getLogger(__name__)

_MDNS_SERVICE = "_mqtt._tcp.local."


@dataclass(frozen=True)
class DiscoveryResult:
    """A reachable MQTT broker candidate.

    `source` is ``"ampio.local"`` for the hostname probe and ``"mdns"`` for
    zeroconf answers. It is a hint, not proof - confirm identity with
    ``AmpioClient.test_connection`` once credentials are known.
    """

    host: str
    port: int
    address: str | None
    source: str
    name: str | None = None


async def discover(
    *,
    hostname: str = "ampio.local",
    port: int = 1883,
    timeout: float = 2.0,
    include_mdns: bool = True,
) -> list[DiscoveryResult]:
    """Return reachable MQTT broker candidates, best-first.

    Always tries `hostname` on `port` via TCP. When `include_mdns` is true and
    the optional `zeroconf` extra is installed, also browses
    ``_mqtt._tcp.local.`` for additional brokers. Candidates with the same
    (address, port) are deduplicated, keeping the hostname result first.

    Returns an empty list if nothing answers; never raises on "not found".
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    tasks: list[asyncio.Task[list[DiscoveryResult]]] = [
        asyncio.create_task(_hostname_strategy(hostname, port, timeout))
    ]
    if include_mdns:
        tasks.append(asyncio.create_task(_browse_mdns(timeout)))

    gathered = await asyncio.gather(*tasks)
    return _dedupe(result for batch in gathered for result in batch)


async def _hostname_strategy(
    host: str, port: int, timeout: float
) -> list[DiscoveryResult]:
    result = await _probe_host(host, port, timeout)
    return [result] if result is not None else []


async def _probe_host(
    host: str, port: int, timeout: float
) -> DiscoveryResult | None:
    """TCP-connect to (host, port); on success resolve the IP for display."""
    try:
        async with asyncio.timeout(timeout):
            _reader, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return None
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass

    address = await _resolve_address(host, port)
    return DiscoveryResult(
        host=host, port=port, address=address, source="ampio.local"
    )


async def _resolve_address(host: str, port: int) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    for family, _type, _proto, _canon, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr:
            return str(sockaddr[0])
    return None


async def _browse_mdns(timeout: float) -> list[DiscoveryResult]:
    """Browse _mqtt._tcp.local. for the given window; empty list on import error."""
    try:
        from zeroconf import ServiceStateChange
        from zeroconf.asyncio import (
            AsyncServiceBrowser,
            AsyncServiceInfo,
            AsyncZeroconf,
        )
    except ImportError:
        _LOGGER.debug("zeroconf not installed; skipping mDNS discovery")
        return []

    found: list[DiscoveryResult] = []
    pending: list[asyncio.Task[None]] = []

    async def _resolve(zc: AsyncZeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zc.zeroconf, int(timeout * 1000)):
            return
        result = _result_from_service_info(info)
        if result is not None:
            found.append(result)

    def _on_change(
        zc: AsyncZeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is not ServiceStateChange.Added:
            return
        pending.append(asyncio.create_task(_resolve(zc, service_type, name)))

    async with AsyncZeroconf() as azc:
        def _bridge(
            _zc: object,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ) -> None:
            _on_change(azc, service_type, name, state_change)

        browser = AsyncServiceBrowser(
            azc.zeroconf, [_MDNS_SERVICE], handlers=[_bridge]
        )
        await asyncio.sleep(timeout)
        await browser.async_cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return found


def _result_from_service_info(info: AsyncServiceInfo) -> DiscoveryResult | None:
    addresses = info.parsed_scoped_addresses()
    if not addresses or info.port is None:
        return None
    host = info.server.rstrip(".") if info.server else addresses[0]
    return DiscoveryResult(
        host=host,
        port=info.port,
        address=addresses[0],
        source="mdns",
        name=info.name.rstrip("."),
    )


def _dedupe(results: Iterable[DiscoveryResult]) -> list[DiscoveryResult]:
    seen: set[tuple[str | None, int]] = set()
    out: list[DiscoveryResult] = []
    for result in results:
        key = (result.address, result.port)
        if key in seen:
            continue
        seen.add(key)
        out.append(result)
    return out
