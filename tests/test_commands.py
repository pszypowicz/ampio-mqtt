"""Tests for the object command surface (`ampio/control/<user>/api`)."""

from __future__ import annotations

import pytest
from conftest import API_TOPIC, DATA_DEVICES_TOPIC, USER, FakeBroker, details, feed

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
        # pulse_ms is wire-encoded in 10 ms units: 1000 ms -> 100, and a
        # non-multiple rounds down: 505 ms -> 50.
        (
            lambda c: c.set_value(111, 255, pulse_ms=1000),
            b"/api/set/111/setValue/255/100",
        ),
        (
            lambda c: c.set_value(111, 255, pulse_ms=505),
            b"/api/set/111/setValue/255/50",
        ),
        (
            lambda c: c.set_color(50, 10, 20, 30, 40),
            b"/api/set/50/setColors/10/20/30/40",
        ),
        (lambda c: c.set_color(50, 1, 2, 3), b"/api/set/50/setColors/1/2/3/0"),
        (lambda c: c.open_cover(48), b"/api/set/48/open"),
        (lambda c: c.close_cover(48), b"/api/set/48/close"),
    ],
)
async def test_helpers_map_to_verified_verbs(
    connected: tuple[AmpioClient, FakeBroker], call, expected: bytes
) -> None:
    client, broker = connected
    await call(client)
    assert broker.published == [(API_TOPIC, expected)]


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda c: c.set_value(111, 0), b"/api/set/111/setValue/0"),
        (lambda c: c.set_value(111, 255), b"/api/set/111/setValue/255"),
        (lambda c: c.set_value(111, 1, pulse_ms=0), b"/api/set/111/setValue/1/0"),
        (
            lambda c: c.set_value(111, 1, pulse_ms=655350),
            b"/api/set/111/setValue/1/65535",
        ),
        (lambda c: c.set_color(50, 0, 0, 0, 0), b"/api/set/50/setColors/0/0/0/0"),
        (
            lambda c: c.set_color(50, 255, 255, 255, 255),
            b"/api/set/50/setColors/255/255/255/255",
        ),
        (lambda c: c.set_cover_position(48, 0), b"/api/set/48/setRollerPos/0/101"),
        (
            lambda c: c.set_cover_position(48, 100, lamella=100),
            b"/api/set/48/setRollerPos/100/100",
        ),
        (lambda c: c.send_event(1), b"/api/setEvent/1"),
        (lambda c: c.send_event(65535), b"/api/setEvent/65535"),
    ],
)
async def test_boundary_values_pass_the_range_checks(
    connected: tuple[AmpioClient, FakeBroker], call, expected: bytes
) -> None:
    """The range limits themselves are legal commands - an off-by-one in
    the range checks must not silently reject them."""
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
    malformed command the M-SERV silently drops."""
    client, broker = connected
    with pytest.raises(ValueError):
        await call(client)
    assert broker.published == []


async def test_command_requires_a_connection() -> None:
    client = AmpioClient("host", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.turn_on(64)


# --- the rgbw switch-verb exception ----------------------------------------


def _learn(client: AmpioClient, oid: int, typ: str) -> None:
    """Teach the store one object's type via a catalogue reply."""
    feed(client, DATA_DEVICES_TOPIC, details({"id": oid, "typ_komponentu": typ}))


async def test_turn_off_on_rgbw_routes_through_set_colors(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """The M-SERV ignores `turnOff` for rgbw objects (no effect, no reply);
    off is unambiguous, so the library sends the one verb that works."""
    client, broker = connected
    _learn(client, 50, "rgbw")
    await client.turn_off(50)
    assert broker.published == [(API_TOPIC, b"/api/set/50/setColors/0/0/0/0")]


@pytest.mark.parametrize(
    "call",
    [lambda c: c.turn_on(50), lambda c: c.toggle(50)],
)
async def test_turn_on_and_toggle_on_rgbw_are_rejected(
    connected: tuple[AmpioClient, FakeBroker], call
) -> None:
    """`turnOn` and `switch` are dropped silently by the M-SERV for rgbw,
    and turning a color light on means choosing a color - the consumer's
    call via `set_color()`. Rejecting before the wire beats a silent no-op,
    exactly as the range checks do."""
    client, broker = connected
    _learn(client, 50, "rgbw")
    with pytest.raises(ValueError):
        await call(client)
    assert broker.published == []


async def test_switch_verbs_pass_through_for_other_outputs(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """Only rgbw carries the exception; a dimmer answers all three verbs."""
    client, broker = connected
    _learn(client, 111, "led")
    await client.turn_on(111)
    await client.toggle(111)
    await client.turn_off(111)
    assert broker.published == [
        (API_TOPIC, b"/api/set/111/turnOn"),
        (API_TOPIC, b"/api/set/111/switch"),
        (API_TOPIC, b"/api/set/111/turnOff"),
    ]


async def test_switch_verbs_pass_through_when_kind_is_unknown(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """Before metadata arrives the library cannot know better than the
    caller, so the plain verbs go out unfiltered."""
    client, broker = connected
    await client.turn_on(99)
    await client.toggle(99)
    await client.turn_off(99)
    assert broker.published == [
        (API_TOPIC, b"/api/set/99/turnOn"),
        (API_TOPIC, b"/api/set/99/switch"),
        (API_TOPIC, b"/api/set/99/turnOff"),
    ]


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


async def test_stop_cover_publishes_the_stop_verb(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    await client.stop_cover(66)
    assert broker.published == [(API_TOPIC, b"/api/set/66/stop")]


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


async def test_set_temperature_publishes_the_setpoint(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    await client.set_temperature(138, 21.5)
    await client.set_temperature(138, 19)
    assert broker.published == [
        (API_TOPIC, b"/api/set/138/setTemperature/21.5"),
        (API_TOPIC, b"/api/set/138/setTemperature/19"),
    ]


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), "21"])
async def test_set_temperature_rejects_non_numbers(
    connected: tuple[AmpioClient, FakeBroker], bad: object
) -> None:
    """Bools and non-finite floats would serialize as text the M-SERV
    silently drops - the same trap the int helpers guard against."""
    client, broker = connected
    with pytest.raises(ValueError):
        await client.set_temperature(138, bad)
    assert broker.published == []
