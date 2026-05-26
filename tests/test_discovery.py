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


# --- Real `_browse_mdns` body coverage --------------------------------------
#
# The tests above stub `_browse_mdns` itself, which leaves the AsyncZeroconf /
# AsyncServiceBrowser / AsyncServiceInfo handling inside the function
# untested. The fakes below stand in for those zeroconf classes so the actual
# `_browse_mdns` body runs and its branches are exercised.


class _FakeAsyncServiceInfo:
    """Stand-in for `zeroconf.asyncio.AsyncServiceInfo`.

    Class-level registries let each test prepare the answer for one or more
    service names; `async_request` returns the pre-seeded answer.
    """

    answers: dict[str, _FakeAsyncServiceInfo] = {}

    def __init__(self, service_type: str, name: str) -> None:
        self.type = service_type
        self.name = name
        seed = _FakeAsyncServiceInfo.answers.get(name)
        self._available = seed is not None
        self.port = seed.port if seed is not None else None
        self.server = seed.server if seed is not None else None
        self._addresses: list[str] = list(seed._addresses) if seed is not None else []

    @classmethod
    def seed(
        cls,
        name: str,
        *,
        port: int,
        server: str | None,
        addresses: list[str],
    ) -> None:
        inst = cls.__new__(cls)
        inst.type = "_mqtt._tcp.local."
        inst.name = name
        inst._available = True
        inst.port = port
        inst.server = server
        inst._addresses = addresses
        cls.answers[name] = inst

    @classmethod
    def reset(cls) -> None:
        cls.answers = {}

    async def async_request(self, _zc: object, _timeout_ms: int) -> bool:
        return self._available

    def parsed_scoped_addresses(self) -> list[str]:
        return list(self._addresses)


class _FakeAsyncServiceBrowser:
    """Fires the configured handlers synchronously on construction."""

    state_change_added: object | None = None

    def __init__(
        self,
        _zc: object,
        _service_types: list[str],
        handlers: list[Any],
    ) -> None:
        for handler in handlers:
            for name in _FakeAsyncServiceBrowser.events_added:
                handler(_zc, "_mqtt._tcp.local.", name, self.state_change_added)
            for name in _FakeAsyncServiceBrowser.events_other:
                handler(_zc, "_mqtt._tcp.local.", name, object())

    async def async_cancel(self) -> None:
        return None

    events_added: list[str] = []
    events_other: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.events_added = []
        cls.events_other = []


class _FakeAsyncZeroconf:
    """Async context manager stand-in for `AsyncZeroconf`."""

    def __init__(self) -> None:
        self.zeroconf = object()

    async def __aenter__(self) -> _FakeAsyncZeroconf:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeServiceStateChange:
    """Enum-like stand-in; `Added` is the value the implementation checks for."""

    Added = object()


def _install_fake_zeroconf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Insert the fake zeroconf modules into sys.modules for the lazy import."""
    _FakeAsyncServiceInfo.reset()
    _FakeAsyncServiceBrowser.reset()
    _FakeAsyncServiceBrowser.state_change_added = _FakeServiceStateChange.Added

    import types

    root = types.ModuleType("zeroconf")
    root.ServiceStateChange = _FakeServiceStateChange  # type: ignore[attr-defined]
    asyncio_mod = types.ModuleType("zeroconf.asyncio")
    asyncio_mod.AsyncServiceBrowser = _FakeAsyncServiceBrowser  # type: ignore[attr-defined]
    asyncio_mod.AsyncServiceInfo = _FakeAsyncServiceInfo  # type: ignore[attr-defined]
    asyncio_mod.AsyncZeroconf = _FakeAsyncZeroconf  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "zeroconf", root)
    monkeypatch.setitem(sys.modules, "zeroconf.asyncio", asyncio_mod)


async def test_browse_mdns_resolves_added_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Added event with a resolvable AsyncServiceInfo becomes a result."""
    _install_fake_zeroconf(monkeypatch)
    _FakeAsyncServiceInfo.seed(
        "broker._mqtt._tcp.local.",
        port=1883,
        server="broker.local.",
        addresses=["192.0.2.99"],
    )
    _FakeAsyncServiceBrowser.events_added = ["broker._mqtt._tcp.local."]

    results = await discovery_module._browse_mdns(timeout=0.01)
    assert results == [
        DiscoveryResult(
            host="broker.local",
            port=1883,
            address="192.0.2.99",
            source="mdns",
            name="broker._mqtt._tcp.local",
        )
    ]


async def test_browse_mdns_ignores_non_added_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removed/Updated events do not create resolve tasks or results."""
    _install_fake_zeroconf(monkeypatch)
    _FakeAsyncServiceBrowser.events_other = ["stale._mqtt._tcp.local."]

    results = await discovery_module._browse_mdns(timeout=0.01)
    assert results == []


async def test_browse_mdns_drops_unresolvable_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Added event whose AsyncServiceInfo.async_request returns False is skipped."""
    _install_fake_zeroconf(monkeypatch)
    # No seed -> async_request returns False.
    _FakeAsyncServiceBrowser.events_added = ["ghost._mqtt._tcp.local."]

    results = await discovery_module._browse_mdns(timeout=0.01)
    assert results == []


async def test_browse_mdns_drops_service_without_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved AsyncServiceInfo with no addresses is also filtered out."""
    _install_fake_zeroconf(monkeypatch)
    _FakeAsyncServiceInfo.seed(
        "empty._mqtt._tcp.local.",
        port=1883,
        server="empty.local.",
        addresses=[],
    )
    _FakeAsyncServiceBrowser.events_added = ["empty._mqtt._tcp.local."]

    results = await discovery_module._browse_mdns(timeout=0.01)
    assert results == []


async def test_browse_mdns_uses_address_when_server_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `info.server` is empty, the first address is used as the host."""
    _install_fake_zeroconf(monkeypatch)
    _FakeAsyncServiceInfo.seed(
        "noserver._mqtt._tcp.local.",
        port=1883,
        server=None,
        addresses=["192.0.2.77"],
    )
    _FakeAsyncServiceBrowser.events_added = ["noserver._mqtt._tcp.local."]

    results = await discovery_module._browse_mdns(timeout=0.01)
    assert results == [
        DiscoveryResult(
            host="192.0.2.77",
            port=1883,
            address="192.0.2.77",
            source="mdns",
            name="noserver._mqtt._tcp.local",
        )
    ]
