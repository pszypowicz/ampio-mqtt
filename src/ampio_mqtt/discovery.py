"""Best-effort discovery of an Ampio M-SERV broker on the LAN.

The M-SERV runs Avahi with default-only hostname publishing: no service type
or TXT record on the LAN identifies it as Ampio (a co-located Matter process
can share its address, but nothing there is Ampio-specific either) - the
hostname A/AAAA record for the well-known name ``ampio.local`` is the only
reliable signal. So discovery is a multicast DNS A-record query for that
hostname, followed by a TCP probe to confirm the broker port is open.

The mDNS query is driven from Python via the ``zeroconf`` package, the
``ampio-mqtt[discovery]`` extra. Callers that already own an
``AsyncZeroconf`` instance (Home Assistant integrations almost always do)
can pass it in via ``zeroconf=...`` to share the multicast socket;
standalone callers can omit the argument and ``discover()`` will spin up
its own per-call.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

try:
    from zeroconf import AddressResolverIPv4, IPVersion
    from zeroconf.asyncio import AsyncZeroconf
except ImportError as _err:
    raise ImportError(
        "LAN discovery needs the optional zeroconf dependency: install the "
        "ampio-mqtt[discovery] extra (Home Assistant provides zeroconf itself)"
    ) from _err


@dataclass(frozen=True)
class DiscoveryResult:
    """A reachable Ampio M-SERV broker candidate.

    The candidate is a hint based on the mDNS hostname probe; confirm
    identity with ``AmpioClient.test_connection`` once credentials are known.
    """

    host: str
    port: int
    address: str


async def discover(
    *,
    hostname: str = "ampio.local",
    port: int = 1883,
    timeout: float = 2.0,
    zeroconf: AsyncZeroconf | None = None,
) -> DiscoveryResult | None:
    """Return the reachable M-SERV candidate found by mDNS-resolving
    ``hostname``, or None.

    A candidate is returned when the hostname answers via mDNS *and* a TCP
    connection to the resolved address succeeds. Never raises on "not
    found".

    ``zeroconf`` lets HA pass its shared ``AsyncZeroconf`` so the discovery
    doesn't open a competing multicast socket. When omitted, a short-lived
    instance is created for the call and closed before returning.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    address = await _resolve_mdns(hostname, timeout * 0.7, zeroconf)
    if address is None:
        return None
    if not await _tcp_probe(address, port, timeout * 0.3):
        return None
    return DiscoveryResult(host=hostname, port=port, address=address)


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
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True
