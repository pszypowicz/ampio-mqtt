"""Tests for ampio_mqtt.discovery.

The hostname strategy is exercised by patching `asyncio.open_connection`
and `loop.getaddrinfo`. There is no second strategy: the M-SERV does not
publish itself via mDNS, so only the hostname probe is implemented.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any
from unittest.mock import patch

import pytest

from ampio_mqtt import DiscoveryResult, discover


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
        results = await discover(timeout=0.5)
    assert results == [
        DiscoveryResult(host="ampio.local", port=1883, address="192.0.2.10")
    ]


async def test_hostname_unreachable_returns_empty() -> None:
    with patch("ampio_mqtt.discovery.asyncio.open_connection", _open_refused):
        results = await discover(timeout=0.5)
    assert results == []


async def test_hostname_timeout_returns_empty() -> None:
    with patch("ampio_mqtt.discovery.asyncio.open_connection", _open_slow):
        results = await discover(timeout=0.05)
    assert results == []


async def test_invalid_timeout_raises() -> None:
    with pytest.raises(ValueError):
        await discover(timeout=0)


async def test_address_resolution_failure_falls_back_to_none() -> None:
    """If getaddrinfo fails, the result still returns with `address=None`."""
    loop = asyncio.get_running_loop()

    async def _gaierror(*_args: Any, **_kwargs: Any) -> Any:
        raise socket.gaierror("no such host")

    with (
        patch("ampio_mqtt.discovery.asyncio.open_connection", _open_ok),
        patch.object(loop, "getaddrinfo", _gaierror),
    ):
        results = await discover(timeout=0.5)
    assert results == [DiscoveryResult(host="ampio.local", port=1883, address=None)]


async def test_writer_close_error_is_swallowed() -> None:
    """A failure during writer.wait_closed() must not prevent a result."""

    class _Raising(_FakeWriter):
        async def wait_closed(self) -> None:
            raise OSError("broken pipe")

    async def _open_with_raising_writer(
        host: str, port: int
    ) -> tuple[object, _Raising]:
        return object(), _Raising()

    loop = asyncio.get_running_loop()
    with (
        patch(
            "ampio_mqtt.discovery.asyncio.open_connection", _open_with_raising_writer
        ),
        patch.object(loop, "getaddrinfo", _patch_getaddrinfo("192.0.2.10")),
    ):
        results = await discover(timeout=0.5)
    assert results == [
        DiscoveryResult(host="ampio.local", port=1883, address="192.0.2.10")
    ]
