"""Best-effort discovery of an Ampio M-SERV broker on the LAN.

Only the well-known `ampio.local` hostname is probed. mDNS service discovery
was considered but is not viable: the M-SERV does not publish a
`_mqtt._tcp.local.` (or any other) service via avahi, so browsing the bus
just surfaces unrelated brokers. Hostname probing is the only path that
reliably identifies an Ampio installation without credentials.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveryResult:
    """A reachable Ampio M-SERV broker candidate.

    The candidate is a hint based on the hostname probe; confirm identity with
    ``AmpioClient.test_connection`` once credentials are known.
    """

    host: str
    port: int
    address: str | None


async def discover(
    *,
    hostname: str = "ampio.local",
    port: int = 1883,
    timeout: float = 2.0,
) -> list[DiscoveryResult]:
    """Return reachable M-SERV candidates by probing ``hostname`` on ``port``.

    Returns a single-element list when the hostname answers a TCP connection,
    and an empty list otherwise. Never raises on "not found".
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    result = await _probe_host(hostname, port, timeout)
    return [result] if result is not None else []


async def _probe_host(host: str, port: int, timeout: float) -> DiscoveryResult | None:
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
    return DiscoveryResult(host=host, port=port, address=address)


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
