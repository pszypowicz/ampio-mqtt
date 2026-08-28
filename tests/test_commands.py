"""Tests for the object command surface (`ampio/control/<user>/api`)."""

from __future__ import annotations

import asyncio

import aiomqtt
import pytest
from conftest import (
    ADMIN_DETAILS_TOPIC,
    ADMIN_DEVICES_TOPIC,
    ADMIN_USER,
    API_TOPIC,
    DATA_DEVICES_TOPIC,
    USER,
    FakeBroker,
    deliver_later,
    details,
    devices,
    feed,
)

from ampio_mqtt import (
    HEATING_MODES,
    AmpioClient,
    AmpioConnectionError,
    AmpioTimeoutError,
)


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


async def test_set_heating_mode_publishes_the_letter(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    for mode in sorted(HEATING_MODES):
        await client.set_heating_mode(138, mode)
    assert broker.published == [
        (API_TOPIC, f"/api/set/138/setHeatingMode/{m}".encode())
        for m in sorted(HEATING_MODES)
    ]


@pytest.mark.parametrize("bad", ["", "s", "X", "SM", "Schedule"])
async def test_set_heating_mode_rejects_unlisted_letters(
    connected: tuple[AmpioClient, FakeBroker], bad: str
) -> None:
    """An unlisted letter raises here instead of being dropped by the
    M-SERV; command() stays the escape hatch for experimenting."""
    client, broker = connected
    with pytest.raises(ValueError):
        await client.set_heating_mode(138, bad)
    assert broker.published == []


# --- confirm: the opt-in state-echo wait (#67) ------------------------------


def _ob_state(oid: int, user: str = USER) -> str:
    """The per-object state topic the echo lands on."""
    return f"ampio/fromDB/{user}/ob/{oid}/state"


async def test_confirm_resolves_with_the_echo_snapshot(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    _learn(client, 64, "przekaznik")
    task = asyncio.create_task(client.set_value(64, 255, confirm=1.0))
    delivery = deliver_later(client, (_ob_state(64), "255"))
    obj = await task
    await delivery
    assert broker.published == [(API_TOPIC, b"/api/set/64/setValue/255")]
    assert obj is not None
    assert (obj.id, obj.state) == (64, "255")


async def test_confirm_ignores_updates_for_other_objects(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, _ = connected
    feed(
        client,
        DATA_DEVICES_TOPIC,
        details(
            {"id": 64, "typ_komponentu": "przekaznik"},
            {"id": 65, "typ_komponentu": "przekaznik"},
        ),
    )
    task = asyncio.create_task(client.turn_on(64, confirm=1.0))
    delivery = deliver_later(client, (_ob_state(65), "255"), (_ob_state(64), "255"))
    obj = await task
    await delivery
    assert obj is not None
    assert obj.id == 64


async def test_confirm_expiry_raises_the_retryable_timeout(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """No echo is how every silent drop surfaces - an ignored verb, an
    out-of-grant object, a no-op command - and the shape is the retryable
    one, exactly like a fetch that got no reply."""
    client, broker = connected
    _learn(client, 64, "przekaznik")
    with pytest.raises(AmpioTimeoutError):
        await client.turn_on(64, confirm=0.01)
    assert broker.published == [(API_TOPIC, b"/api/set/64/turnOn")]
    # The waiter disarms with the call: no stale listener remains to catch
    # a later update.
    assert client._listeners == []


async def test_confirm_defaults_off(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """Without confirm the call is fire-and-forget: returns None as soon as
    the broker acknowledges, arming nothing."""
    client, _ = connected
    assert await client.command(64, "turnOn") is None
    assert client._listeners == []


@pytest.mark.parametrize(
    ("typ", "oid", "call", "expected"),
    [
        (
            "przekaznik",
            64,
            lambda c: c.turn_on(64, confirm=1.0),
            b"/api/set/64/turnOn",
        ),
        # The rgbw off-routing and confirm compose: the echo confirms the
        # setColors command the library substituted.
        (
            "rgbw",
            50,
            lambda c: c.turn_off(50, confirm=1.0),
            b"/api/set/50/setColors/0/0/0/0",
        ),
        (
            "roleta",
            48,
            lambda c: c.set_cover_position(48, 55, confirm=1.0),
            b"/api/set/48/setRollerPos/55/101",
        ),
    ],
)
async def test_wrappers_thread_confirm_through(
    connected: tuple[AmpioClient, FakeBroker], typ: str, oid: int, call, expected: bytes
) -> None:
    client, broker = connected
    _learn(client, oid, typ)
    task = asyncio.create_task(call(client))
    delivery = deliver_later(client, (_ob_state(oid), "1"))
    obj = await task
    await delivery
    assert broker.published == [(API_TOPIC, expected)]
    assert obj is not None
    assert obj.id == oid


async def test_confirm_survives_the_catalogue_race(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A command sent before any catalogue establishes the object still
    confirms: its echo waits in the pending buffer and surfaces with the
    catalogue row, so a consumer commanding right after connect is not
    condemned to a spurious timeout."""
    client, _ = connected
    task = asyncio.create_task(client.set_value(70, 255, confirm=1.0))
    await asyncio.sleep(0)  # the waiter arms before the publish
    feed(client, _ob_state(70), "255")
    assert not task.done()
    feed(client, DATA_DEVICES_TOPIC, details({"id": 70, "typ_komponentu": "flaga"}))
    obj = await task
    assert obj is not None
    assert (obj.id, obj.state) == (70, "255")


async def test_concurrent_confirms_resolve_on_one_echo(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, _ = connected
    _learn(client, 64, "przekaznik")
    first = asyncio.create_task(client.turn_on(64, confirm=1.0))
    second = asyncio.create_task(client.turn_on(64, confirm=1.0))
    delivery = deliver_later(client, (_ob_state(64), "255"))
    one, other = await asyncio.gather(first, second)
    await delivery
    assert one == other
    assert one is not None
    assert one.state == "255"


async def test_confirm_propagates_a_publish_failure(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A transport failure keeps its own type - the confirm window must not
    relabel it as the retryable echo timeout."""
    client, broker = connected
    _learn(client, 64, "przekaznik")
    broker.publish_errors = [aiomqtt.MqttError("boom")]
    with pytest.raises(AmpioConnectionError) as excinfo:
        await client.turn_on(64, confirm=1.0)
    assert not isinstance(excinfo.value, AmpioTimeoutError)
    assert client._listeners == []


async def test_confirm_on_the_admin_tier_resolves_on_the_raw_edge() -> None:
    """The store drops a raw-proven object's per-object echo whole, so the
    raw edge must satisfy the wait - any-source ObjectUpdated is the
    primitive, or admin-tier confirms on bridged inputs would always
    time out."""
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        feed(
            client,
            ADMIN_DEVICES_TOPIC,
            devices(
                {
                    "id": 7,
                    "mac": 0xCAFE,
                    "typ_urzadzenia": 11,
                    "nazwa_urzadzenia": "panel",
                }
            ),
        )
        feed(
            client,
            ADMIN_DETAILS_TOPIC,
            details(
                {
                    "id": 10,
                    "id_urzadzenia": 7,
                    "typ_komponentu": "flaga",
                    "interpretacja": 1,
                    "funkcja": 3,
                    "opis_menu": "Flag",
                }
            ),
        )
        # One raw edge proves the raw path owns the object from here on.
        feed(client, "ampio/from/CAFE/state/f/3", "0")
        task = asyncio.create_task(client.set_value(10, 255, confirm=1.0))
        await asyncio.sleep(0)
        feed(client, "ampio/from/CAFE/state/f/3", "255")
        obj = await task
        assert obj is not None
        assert (obj.id, obj.state) == (10, "255")
    finally:
        await client.stop()


# --- panel outputs (the raw CAN write path) --------------------------------

ADMIN_API_TOPIC = f"ampio/control/{ADMIN_USER}/api"
PANEL_RAW_TOPIC = "ampio/to/cafe/raw"


async def _admin_with_panel_output() -> tuple[AmpioClient, FakeBroker]:
    """Admin client whose catalogue holds a panel LED (90) and a relay (91)."""
    broker = FakeBroker()
    client = AmpioClient(
        "host", username=ADMIN_USER, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.01)
    feed(
        client,
        ADMIN_DEVICES_TOPIC,
        devices(
            {"id": 7, "mac": 0xCAFE, "typ_urzadzenia": 11, "nazwa_urzadzenia": "p"},
            {"id": 8, "mac": 0xB0B0, "typ_urzadzenia": 4, "nazwa_urzadzenia": "r"},
        ),
    )
    feed(
        client,
        ADMIN_DETAILS_TOPIC,
        details(
            {
                "id": 90,
                "id_urzadzenia": 7,
                "typ_komponentu": "przekaznik",
                "interpretacja": 2,
                "funkcja": 2,
                "leafId": "0_cafe_257_2_1",
                "opis_menu": "LED",
            },
            {
                "id": 91,
                "id_urzadzenia": 8,
                "typ_komponentu": "przekaznik",
                "interpretacja": 1,
                "funkcja": 1,
                "leafId": "0_b0b0_257_2_0",
                "opis_menu": "Relay",
            },
        ),
    )
    broker.published.clear()
    broker.published_qos.clear()
    return client, broker


async def test_panel_output_switch_verbs_ride_the_raw_frame() -> None:
    """The frame channel is the 0-based leaf_out_no, one below funkcja."""
    client, broker = await _admin_with_panel_output()
    try:
        await client.turn_on(90)
        await client.turn_off(90)
        await client.set_value(90, 128)
        assert broker.published == [
            (PANEL_RAW_TOPIC, b"30f9ff01"),
            (PANEL_RAW_TOPIC, b"30f90001"),
            (PANEL_RAW_TOPIC, b"30f98001"),
        ]
        assert broker.published_qos == [1, 1, 1]
    finally:
        await client.stop()


async def test_panel_output_toggle_inverts_the_held_state() -> None:
    client, broker = await _admin_with_panel_output()
    try:
        await client.toggle(90)  # no value yet -> reads off -> turns on
        feed(client, "ampio/from/CAFE/state/o/2", "1")
        await client.toggle(90)
        assert broker.published == [
            (PANEL_RAW_TOPIC, b"30f9ff01"),
            (PANEL_RAW_TOPIC, b"30f90001"),
        ]
    finally:
        await client.stop()


async def test_pulse_always_rides_api() -> None:
    """The raw frame has no timed form, so a pulse keeps the /api path -
    which a panel output ignores; confirm= is what surfaces that."""
    client, broker = await _admin_with_panel_output()
    try:
        await client.set_value(90, 255, pulse_ms=500)
        assert broker.published == [(ADMIN_API_TOPIC, b"/api/set/90/setValue/255/50")]
    finally:
        await client.stop()


async def test_relay_output_rides_the_raw_frame_on_admin_too() -> None:
    """Dumb routing: every CAN-module przekaznik uses the raw frame on the
    admin tier - the frame is the generic output write, proven on relays."""
    client, broker = await _admin_with_panel_output()
    try:
        await client.turn_on(91)
        assert broker.published == [("ampio/to/b0b0/raw", b"30f9ff00")]
    finally:
        await client.stop()


async def test_server_owned_output_keeps_the_api_path() -> None:
    """The M-SERV's own virtual outputs live in its DB, not on the CAN
    bus - /api stays their surface on every tier."""
    client, broker = await _admin_with_panel_output()
    try:
        feed(
            client,
            ADMIN_DETAILS_TOPIC,
            details(
                {
                    "id": 92,
                    "id_urzadzenia": 1,
                    "typ_komponentu": "przekaznik",
                    "interpretacja": 1,
                    "funkcja": 1,
                    "leafId": "0_1_257_2_0",
                    "opis_menu": "Virtual",
                }
            ),
        )
        broker.published.clear()
        await client.turn_on(92)
        assert broker.published == [(ADMIN_API_TOPIC, b"/api/set/92/turnOn")]
    finally:
        await client.stop()


async def test_restricted_tier_keeps_the_api_path_for_panel_objects(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """The raw write tree is admin-only, so the restricted tier publishes
    the /api form - which the M-SERV drops for a panel output, surfaced
    by confirm=. Documented as an Ampio limitation in protocol.md."""
    client, broker = connected
    feed(
        client,
        DATA_DEVICES_TOPIC,
        details(
            {
                "id": 90,
                "id_urzadzenia": 7,
                "typ_komponentu": "przekaznik",
                "interpretacja": 2,
                "funkcja": 2,
                "leafId": "0_cafe_257_2_1",
                "opis_menu": "LED",
            }
        ),
    )
    await client.turn_on(90)
    assert broker.published == [(API_TOPIC, b"/api/set/90/turnOn")]


async def test_panel_output_confirm_resolves_on_the_raw_edge() -> None:
    client, _broker = await _admin_with_panel_output()
    try:
        task = asyncio.create_task(client.turn_on(90, confirm=1.0))
        await asyncio.sleep(0)
        feed(client, "ampio/from/CAFE/state/o/2", "1")
        obj = await task
        assert obj is not None
        assert (obj.id, obj.state) == (90, "1")
    finally:
        await client.stop()


async def test_admin_subscribes_the_o_wildcard_and_restricted_does_not(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    admin_client, admin_broker = await _admin_with_panel_output()
    try:
        assert "ampio/from/+/state/o/+" in admin_broker.subscribed
    finally:
        await admin_client.stop()
    _, restricted_broker = connected
    assert "ampio/from/+/state/o/+" not in restricted_broker.subscribed


# --- flags as switch targets (#125) ----------------------------------------


async def test_flag_switch_verbs_ride_api_on_the_admin_tier() -> None:
    """A flag answers the switch family over `/api`, and never over the raw
    write topic - the raw output frame addresses the module's output
    channels, a space a flag index does not belong to."""
    client, broker = await _admin_with_panel_output()
    try:
        feed(
            client,
            ADMIN_DETAILS_TOPIC,
            details(
                {
                    "id": 93,
                    "id_urzadzenia": 7,
                    "typ_komponentu": "flaga",
                    "leafId": "0_cafe_3_0_23",
                    "opis_menu": "Flag",
                }
            ),
        )
        broker.published.clear()
        await client.turn_on(93)
        await client.toggle(93)
        await client.turn_off(93)
        assert broker.published == [
            (ADMIN_API_TOPIC, b"/api/set/93/turnOn"),
            (ADMIN_API_TOPIC, b"/api/set/93/switch"),
            (ADMIN_API_TOPIC, b"/api/set/93/turnOff"),
        ]
    finally:
        await client.stop()


async def test_flag_switch_verbs_ride_api_on_the_restricted_tier(
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    client, broker = connected
    _learn(client, 70, "flaga")
    await client.turn_on(70)
    await client.toggle(70)
    await client.turn_off(70)
    assert broker.published == [
        (API_TOPIC, b"/api/set/70/turnOn"),
        (API_TOPIC, b"/api/set/70/switch"),
        (API_TOPIC, b"/api/set/70/turnOff"),
    ]
