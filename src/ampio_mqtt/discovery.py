"""Best-effort discovery of an Ampio M-SERV broker on the LAN.

The M-SERV runs Avahi with default-only hostname publishing: no service type,
no TXT records identifying it as Ampio - only an A/AAAA record for the
well-known hostname ``ampio.local``. So discovery is a multicast DNS A-record
query for that hostname, followed by a TCP probe to confirm the broker port
is open.

The mDNS query is driven from Python via the ``zeroconf`` package, which is a
hard runtime dependency. Callers that already own an ``AsyncZeroconf``
instance (Home Assistant integrations almost always do) can pass it in via
``zeroconf=...`` to share the multicast socket; standalone callers can omit
the argument and ``discover()`` will spin up its own per-call.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

from zeroconf import AddressResolverIPv4, IPVersion
from zeroconf.asyncio import AsyncZeroconf


@dataclass(frozen=True)
class DiscoveryResult:
    """A reachable Ampio M-SERV broker candidate.

    The candidate is a hint based on the mDNS hostname probe; confirm
    identity with ``AmpioClient.test_connection`` once credentials are known.
    """

    host: str
    port: int
    address: str | None


async def discover(
    *,
    hostname: str = "ampio.local",
    port: int = 1883,
    timeout: float = 2.0,
    zeroconf: AsyncZeroconf | None = None,
) -> list[DiscoveryResult]:
    """Return reachable M-SERV candidates by mDNS-resolving ``hostname``.

    Returns a single-element list when the hostname answers via mDNS *and* a
    TCP connection to the resolved address succeeds, and an empty list
    otherwise. Never raises on "not found".

    ``zeroconf`` lets HA pass its shared ``AsyncZeroconf`` so the discovery
    doesn't open a competing multicast socket. When omitted, a short-lived
    instance is created for the call and closed before returning.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    result = await _probe_host(hostname, port, timeout, zeroconf)
    return [result] if result is not None else []


async def _probe_host(
    host: str, port: int, timeout: float, zc: AsyncZeroconf | None
) -> DiscoveryResult | None:
    """Resolve `host` via mDNS, then TCP-probe the resolved address on `port`."""
    address = await _resolve_mdns(host, timeout * 0.7, zc)
    if address is None:
        return None
    if not await _tcp_probe(address, port, timeout * 0.3):
        return None
    return DiscoveryResult(host=host, port=port, address=address)


async def _resolve_mdns(
    hostname: str, timeout: float, zc: AsyncZeroconf | None
) -> str | None:
    """Issue a multicast DNS A-record query for `hostname`, return IPv4 or None.

    `timeout` is the budget in seconds; zeroconf takes ms.
    """
    fqdn = hostname if hostname.endswith(".") else f"{hostname}."
    async with _zeroconf_context(zc) as azc:
        resolver = AddressResolverIPv4(fqdn)
        if not await resolver.async_request(azc.zeroconf, int(timeout * 1000)):
            return None
        addrs = resolver.ip_addresses_by_version(IPVersion.V4Only)
        return str(addrs[0]) if addrs else None


@contextlib.asynccontextmanager
async def _zeroconf_context(
    zc: AsyncZeroconf | None,
) -> AsyncIterator[AsyncZeroconf]:
    """Yield `zc` if the caller owns it, else create and close one ourselves."""
    if zc is not None:
        yield zc
        return
    async with AsyncZeroconf() as owned:
        yield owned


async def _tcp_probe(address: str, port: int, timeout: float) -> bool:
    """TCP-connect to (address, port); return True on success."""
    try:
        async with asyncio.timeout(timeout):
            _reader, writer = await asyncio.open_connection(address, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True
