"""Tests for the object command surface (`ampio/control/<user>/api`)."""

from __future__ import annotations

import pytest

from ampio_mqtt import AmpioClient, AmpioConnectionError

USER = "u"
TOPIC = f"ampio/control/{USER}/api"


class _RecordingClient:
    """Captures publishes in place of the real aiomqtt client."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, payload: bytes = b"") -> None:
        self.published.append((topic, payload))


def _connected_client() -> tuple[AmpioClient, _RecordingClient]:
    client = AmpioClient("host", username=USER)
    recorder = _RecordingClient()
    client._client = recorder  # type: ignore[assignment]
    return client, recorder


async def test_command_builds_payload_on_the_account_topic() -> None:
    client, recorder = _connected_client()
    await client.command(64, "setValue", 255)
    assert recorder.published == [(TOPIC, b"/api/set/64/setValue/255")]


async def test_command_without_args_omits_trailing_slash() -> None:
    client, recorder = _connected_client()
    await client.command(64, "turnOn")
    assert recorder.published == [(TOPIC, b"/api/set/64/turnOn")]


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda c: c.turn_on(64), b"/api/set/64/turnOn"),
        (lambda c: c.turn_off(64), b"/api/set/64/turnOff"),
        (lambda c: c.toggle(64), b"/api/set/64/switch"),
        (lambda c: c.set_value(111, 128), b"/api/set/111/setValue/128"),
        # pulse_ms is wire-encoded in 10 ms units: 1000 ms -> 100.
        (
            lambda c: c.set_value(111, 255, pulse_ms=1000),
            b"/api/set/111/setValue/255/100",
        ),
        (
            lambda c: c.set_color(50, 10, 20, 30, 40),
            b"/api/set/50/setColors/10/20/30/40",
        ),
        (lambda c: c.set_color(50, 1, 2, 3), b"/api/set/50/setColors/1/2/3/0"),
        (lambda c: c.open_cover(48), b"/api/set/48/open"),
        (lambda c: c.close_cover(48), b"/api/set/48/close"),
        # No lamella given -> 101, the M-SERV's "leave this axis alone".
        (lambda c: c.set_cover_position(48, 55), b"/api/set/48/setRollerPos/55/101"),
        (
            lambda c: c.set_cover_position(48, 55, lamella=20),
            b"/api/set/48/setRollerPos/55/20",
        ),
    ],
)
async def test_helpers_map_to_verified_verbs(call, expected: bytes) -> None:
    client, recorder = _connected_client()
    await call(client)
    assert recorder.published == [(TOPIC, expected)]


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.set_value(1, 256),
        lambda c: c.set_value(1, -1),
        lambda c: c.set_value(1, 10, pulse_ms=-5),
        lambda c: c.set_color(1, 0, 0, 300),
        lambda c: c.set_cover_position(1, 101),
        lambda c: c.set_cover_position(1, 50, lamella=200),
    ],
)
async def test_out_of_range_arguments_are_rejected(call) -> None:
    client, recorder = _connected_client()
    with pytest.raises(ValueError):
        await call(client)
    assert recorder.published == []


async def test_command_requires_a_connection() -> None:
    client = AmpioClient("host", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.turn_on(64)
