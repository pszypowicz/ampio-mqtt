"""Tests for ampio_mqtt.discovery.

The hostname strategy is exercised by patching `asyncio.open_connection`
and `loop.getaddrinfo`. The mDNS strategy is exercised by stubbing the
`zeroconf` modules in `sys.modules` so the lazy import inside
`_browse_mdns` resolves to a controlled fake.
"""

from __future__ import annotations

import asyncio
import builtins
import socket
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ampio_mqtt import DiscoveryResult, discover
from ampio_mqtt import discovery as discovery_module


class _FakeWriter:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        return None


async def _open_ok(host: str, port: int) -> tuple[object, _FakeWriter]:
    return object(), _FakeWriter()


async def _open_refused(host: str, port: int) -> tuple[object, _FakeWriter]:
    raise OSError("connection refused")


async def _open_slow(host: str, port: int) -> tuple[object, _FakeWriter]:
    await asyncio.sleep(10)
    return object(), _FakeWriter()


def _patch_getaddrinfo(address: str) -> Any:
    async def _fake_getaddrinfo(
        host: str, port: int, **kwargs: Any
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    return _fake_getaddrinfo


async def test_hostname_reachable_returns_single_result() -> None:
    loop = asyncio.get_running_loop()
    with (
        patch("ampio_mqtt.discovery.asyncio.open_connection", _open_ok),
        patch.object(loop, "getaddrinfo", _patch_getaddrinfo("192.0.2.10")),
    ):
        results = await discover(include_mdns=False, timeout=0.5)
    assert results == [
        DiscoveryResult(
            host="ampio.local",
            port=1883,
            address="192.0.2.10",
            source="ampio.local",
        )
    ]


async def test_hostname_unreachable_returns_empty() -> None:
    with patch("ampio_mqtt.discovery.asyncio.open_connection", _open_refused):
        results = await discover(include_mdns=False, timeout=0.5)
    assert results == []


async def test_hostname_timeout_returns_empty() -> None:
    with patch("ampio_mqtt.discovery.asyncio.open_connection", _open_slow):
        results = await discover(include_mdns=False, timeout=0.05)
    assert results == []


async def test_invalid_timeout_raises() -> None:
    with pytest.raises(ValueError):
        await discover(timeout=0, include_mdns=False)


async def test_include_mdns_false_does_not_import_zeroconf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When mDNS is disabled, the zeroconf import path must not run."""
    monkeypatch.delitem(sys.modules, "zeroconf", raising=False)
    monkeypatch.delitem(sys.modules, "zeroconf.asyncio", raising=False)
    browse_mock = AsyncMock(return_value=[])
    with (
        patch("ampio_mqtt.discovery.asyncio.open_connection", _open_refused),
        patch.object(discovery_module, "_browse_mdns", browse_mock),
    ):
        await discover(include_mdns=False, timeout=0.1)
    browse_mock.assert_not_called()
    assert "zeroconf" not in sys.modules
    assert "zeroconf.asyncio" not in sys.modules


async def test_zeroconf_missing_falls_back_to_hostname_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ImportError inside _browse_mdns yields [] without bubbling up."""
    real_import = builtins.__import__

    def _block_zeroconf(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "zeroconf" or name.startswith("zeroconf."):
            raise ImportError(f"No module named '{name}'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _block_zeroconf)
    monkeypatch.delitem(sys.modules, "zeroconf", raising=False)
    monkeypatch.delitem(sys.modules, "zeroconf.asyncio", raising=False)

    loop = asyncio.get_running_loop()
    with (
        patch("ampio_mqtt.discovery.asyncio.open_connection", _open_ok),
        patch.object(loop, "getaddrinfo", _patch_getaddrinfo("192.0.2.10")),
    ):
        results = await discover(timeout=0.2)
    assert results == [
        DiscoveryResult(
            host="ampio.local",
            port=1883,
            address="192.0.2.10",
            source="ampio.local",
        )
    ]


async def test_mdns_adds_unique_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """An mDNS hit on a different address appears as a second candidate."""

    async def fake_browse(_timeout: float) -> list[DiscoveryResult]:
        return [
            DiscoveryResult(
                host="other.local",
                port=1883,
                address="192.0.2.55",
                source="mdns",
                name="other._mqtt._tcp.local",
            )
        ]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(discovery_module, "_browse_mdns", fake_browse)
    with (
        patch("ampio_mqtt.discovery.asyncio.open_connection", _open_ok),
        patch.object(loop, "getaddrinfo", _patch_getaddrinfo("192.0.2.10")),
    ):
        results = await discover(timeout=0.2)
    assert len(results) == 2
    assert results[0].source == "ampio.local"
    assert results[1] == DiscoveryResult(
        host="other.local",
        port=1883,
        address="192.0.2.55",
        source="mdns",
        name="other._mqtt._tcp.local",
    )


async def test_mdns_duplicate_address_is_deduped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If mDNS resolves the same address as the hostname, drop the duplicate."""

    async def fake_browse(_timeout: float) -> list[DiscoveryResult]:
        return [
            DiscoveryResult(
                host="ampio.local",
                port=1883,
                address="192.0.2.10",
                source="mdns",
                name="ampio._mqtt._tcp.local",
            )
        ]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(discovery_module, "_browse_mdns", fake_browse)
    with (
        patch("ampio_mqtt.discovery.asyncio.open_connection", _open_ok),
        patch.object(loop, "getaddrinfo", _patch_getaddrinfo("192.0.2.10")),
    ):
        results = await discover(timeout=0.2)
    assert len(results) == 1
    assert results[0].source == "ampio.local"
