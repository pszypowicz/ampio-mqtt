"""Tests for ampio_mqtt.discovery.

Two orthogonal layers are exercised:

- `discover()` and `_probe_host` orchestration, by stubbing `_resolve_mdns`
  and `_tcp_probe` directly. These confirm the four arm-paths (timeout-raise,
  mDNS-fails-early, TCP-fails, full success) and that an externally-provided
  `AsyncZeroconf` reaches the resolver call without being closed.
- `_resolve_mdns` and `_tcp_probe` internals, by stubbing the zeroconf and
  asyncio.open_connection calls they make. These confirm the integration with
  the underlying APIs without standing up an actual mDNS responder.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from zeroconf.asyncio import AsyncZeroconf  # noqa: F401  (re-exported by signatures below)

from ampio_mqtt import DiscoveryResult, discover
from ampio_mqtt import discovery as discovery_mod


# --- discover() / _probe_host orchestration --------------------------------


async def test_invalid_timeout_raises() -> None:
    with pytest.raises(ValueError):
        await discover(timeout=0)


async def test_mdns_fails_returns_empty() -> None:
    async def _resolve(host: str, timeout: float, zc: AsyncZeroconf | None) -> None:
        return None

    with patch.object(discovery_mod, "_resolve_mdns", _resolve):
        results = await discover(timeout=0.5)
    assert results == []


async def test_mdns_ok_but_tcp_probe_fails_returns_empty() -> None:
    async def _resolve(host: str, timeout: float, zc: AsyncZeroconf | None) -> str:
        return "192.0.2.10"

    async def _probe(address: str, port: int, timeout: float) -> bool:
        return False

    with (
        patch.object(discovery_mod, "_resolve_mdns", _resolve),
        patch.object(discovery_mod, "_tcp_probe", _probe),
    ):
        results = await discover(timeout=0.5)
    assert results == []


async def test_mdns_and_tcp_ok_returns_single_result() -> None:
    async def _resolve(host: str, timeout: float, zc: AsyncZeroconf | None) -> str:
        return "192.0.2.10"

    async def _probe(address: str, port: int, timeout: float) -> bool:
        return True

    with (
        patch.object(discovery_mod, "_resolve_mdns", _resolve),
        patch.object(discovery_mod, "_tcp_probe", _probe),
    ):
        results = await discover(timeout=0.5)
    assert results == [
        DiscoveryResult(host="ampio.local", port=1883, address="192.0.2.10")
    ]


async def test_external_zeroconf_is_passed_through_to_resolver() -> None:
    """Caller-owned AsyncZeroconf must reach `_resolve_mdns` unchanged.

    The "must not be closed" half of the contract is covered by
    `test_resolve_mdns_uses_external_zeroconf_without_closing_it`, which
    exercises the real `_zeroconf_context` against a fake instance whose
    close state is observable.
    """
    sentinel = object()
    seen: list[object] = []

    async def _resolve(host: str, timeout: float, zc: object) -> None:
        seen.append(zc)
        return None

    with patch.object(discovery_mod, "_resolve_mdns", _resolve):
        await discover(timeout=0.5, zeroconf=sentinel)  # type: ignore[arg-type]
    assert seen == [sentinel]


# --- _resolve_mdns internals ----------------------------------------------


class _FakeResolver:
    def __init__(self, found: bool, addresses: list[str]) -> None:
        self._found = found
        self._addresses = addresses
        self.requested_with: tuple[Any, int] | None = None

    async def async_request(self, zc: Any, timeout_ms: int) -> bool:
        self.requested_with = (zc, timeout_ms)
        return self._found

    def ip_addresses_by_version(self, version: Any) -> list[str]:
        return list(self._addresses)


class _FakeAsyncZeroconf:
    def __init__(self) -> None:
        self.zeroconf = object()
        self.closed = False

    async def __aenter__(self) -> _FakeAsyncZeroconf:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True


async def test_resolve_mdns_returns_address_on_success() -> None:
    resolver = _FakeResolver(found=True, addresses=["192.0.2.10"])
    fake_azc = _FakeAsyncZeroconf()
    with (
        patch.object(discovery_mod, "AddressResolverIPv4", lambda fqdn: resolver),
        patch.object(discovery_mod, "AsyncZeroconf", lambda: fake_azc),
    ):
        address = await discovery_mod._resolve_mdns("ampio.local", timeout=0.5, zc=None)

    assert address == "192.0.2.10"
    assert resolver.requested_with == (fake_azc.zeroconf, 500)
    assert fake_azc.closed  # internally-created instance must be cleaned up


async def test_resolve_mdns_uses_external_zeroconf_without_closing_it() -> None:
    resolver = _FakeResolver(found=True, addresses=["192.0.2.10"])
    external = _FakeAsyncZeroconf()
    with patch.object(discovery_mod, "AddressResolverIPv4", lambda fqdn: resolver):
        address = await discovery_mod._resolve_mdns(
            "ampio.local", timeout=0.5, zc=external
        )

    assert address == "192.0.2.10"
    assert resolver.requested_with == (external.zeroconf, 500)
    assert not external.closed  # caller-owned instance must NOT be closed


async def test_resolve_mdns_request_unanswered_returns_none() -> None:
    resolver = _FakeResolver(found=False, addresses=[])
    fake_azc = _FakeAsyncZeroconf()
    with (
        patch.object(discovery_mod, "AddressResolverIPv4", lambda fqdn: resolver),
        patch.object(discovery_mod, "AsyncZeroconf", lambda: fake_azc),
    ):
        address = await discovery_mod._resolve_mdns("ampio.local", timeout=0.5, zc=None)
    assert address is None


async def test_resolve_mdns_request_returns_true_but_no_addresses() -> None:
    """Defensive: zeroconf says it answered but has no IPv4 records to give."""
    resolver = _FakeResolver(found=True, addresses=[])
    fake_azc = _FakeAsyncZeroconf()
    with (
        patch.object(discovery_mod, "AddressResolverIPv4", lambda fqdn: resolver),
        patch.object(discovery_mod, "AsyncZeroconf", lambda: fake_azc),
    ):
        address = await discovery_mod._resolve_mdns("ampio.local", timeout=0.5, zc=None)
    assert address is None


async def test_resolve_mdns_appends_trailing_dot() -> None:
    """mDNS names are fully-qualified; `_resolve_mdns` must add the dot."""
    captured: list[str] = []

    def _capturing_factory(fqdn: str) -> _FakeResolver:
        captured.append(fqdn)
        return _FakeResolver(found=False, addresses=[])

    fake_azc = _FakeAsyncZeroconf()
    with (
        patch.object(discovery_mod, "AddressResolverIPv4", _capturing_factory),
        patch.object(discovery_mod, "AsyncZeroconf", lambda: fake_azc),
    ):
        await discovery_mod._resolve_mdns("ampio.local", timeout=0.5, zc=None)
        await discovery_mod._resolve_mdns("ampio.local.", timeout=0.5, zc=None)
    assert captured == ["ampio.local.", "ampio.local."]


# --- _tcp_probe internals --------------------------------------------------


class _FakeWriter:
    def __init__(self, close_raises: bool = False) -> None:
        self._close_raises = close_raises

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        if self._close_raises:
            raise OSError("broken pipe")


async def test_tcp_probe_success() -> None:
    async def _open(host: str, port: int) -> tuple[object, _FakeWriter]:
        return object(), _FakeWriter()

    with patch("ampio_mqtt.discovery.asyncio.open_connection", _open):
        ok = await discovery_mod._tcp_probe("192.0.2.10", 1883, timeout=0.5)
    assert ok is True


async def test_tcp_probe_refused_returns_false() -> None:
    async def _open(host: str, port: int) -> tuple[object, _FakeWriter]:
        raise OSError("connection refused")

    with patch("ampio_mqtt.discovery.asyncio.open_connection", _open):
        ok = await discovery_mod._tcp_probe("192.0.2.10", 1883, timeout=0.5)
    assert ok is False


async def test_tcp_probe_timeout_returns_false() -> None:
    async def _open(host: str, port: int) -> tuple[object, _FakeWriter]:
        await asyncio.sleep(10)
        return object(), _FakeWriter()

    with patch("ampio_mqtt.discovery.asyncio.open_connection", _open):
        ok = await discovery_mod._tcp_probe("192.0.2.10", 1883, timeout=0.05)
    assert ok is False


async def test_tcp_probe_writer_close_error_is_swallowed() -> None:
    """A failure during writer.wait_closed() must not flip success to failure."""

    async def _open(host: str, port: int) -> tuple[object, _FakeWriter]:
        return object(), _FakeWriter(close_raises=True)

    with patch("ampio_mqtt.discovery.asyncio.open_connection", _open):
        ok = await discovery_mod._tcp_probe("192.0.2.10", 1883, timeout=0.5)
    assert ok is True
