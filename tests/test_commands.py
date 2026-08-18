"""Tests for the object command surface (`ampio/control/<user>/api`)."""

from __future__ import annotations

import pytest
from conftest import API_TOPIC, USER, FakeBroker

from ampio_mqtt import AmpioClient, AmpioConnectionError


async def test_command_builds_payload_on_the_account_topic(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    await client.command(64, "setValue", 255)
    assert broker.published == [(API_TOPIC, b"/api/set/64/setValue/255")]
    # Commands publish at QoS 1 so returning means the broker accepted the
    # command (#68).
    assert broker.published_qos == [1]


async def test_command_without_args_omits_trailing_slash(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    await client.command(64, "turnOn")
    assert broker.published == [(API_TOPIC, b"/api/set/64/turnOn")]


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
async def test_helpers_map_to_verified_verbs(
    connected: tuple[AmpioClient, FakeBroker], call, expected: bytes
) -> None:
    client, broker = connected
    await call(client)
    assert broker.published == [(API_TOPIC, expected)]


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
async def test_out_of_range_arguments_are_rejected(
    connected: tuple[AmpioClient, FakeBroker], call
) -> None:
    client, broker = connected
    with pytest.raises(ValueError):
        await call(client)
    assert broker.published == []


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.set_value(1, True),
        lambda c: c.set_value(1, 10, pulse_ms=True),
        lambda c: c.set_color(1, True, 0, 0),
        lambda c: c.set_cover_position(1, False),
        lambda c: c.set_cover_tilt(1, True),
        lambda c: c.send_event(True),
    ],
)
async def test_bool_arguments_are_rejected(
    connected: tuple[AmpioClient, FakeBroker], call
) -> None:
    """bool passes isinstance(int) and the type checker, but the wire
    encoding is str(), so it would go out as the literal 'True' - a
    malformed command the M-SERV silently drops (live-verified)."""
    client, broker = connected
    with pytest.raises(ValueError):
        await call(client)
    assert broker.published == []


async def test_command_requires_a_connection() -> None:
    client = AmpioClient("host", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.turn_on(64)


# --- cover tilt ------------------------------------------------------------


async def test_position_only_move_leaves_the_slats_alone(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """Both cover types take the sentinel on the axis that must not move."""
    client, broker = connected
    await client.set_cover_position(48, 55)
    await client.set_cover_position(66, 95)
    assert broker.published == [
        (API_TOPIC, b"/api/set/48/setRollerPos/55/101"),
        (API_TOPIC, b"/api/set/66/setRollerPos/95/101"),
    ]


async def test_tilt_only_move_leaves_the_position_alone(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    await client.set_cover_tilt(66, 50)
    assert broker.published == [(API_TOPIC, b"/api/set/66/setRollerPos/101/50")]


async def test_both_axes_move_in_one_command(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    await client.set_cover_position(66, 95, lamella=20)
    assert broker.published == [(API_TOPIC, b"/api/set/66/setRollerPos/95/20")]


@pytest.mark.parametrize("lamella", [-1, 101, 200])
async def test_tilt_range_is_checked(
    connected: tuple[AmpioClient, FakeBroker], lamella: int
) -> None:
    client, broker = connected
    with pytest.raises(ValueError):
        await client.set_cover_tilt(66, lamella)
    assert broker.published == []
