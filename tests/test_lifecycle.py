"""Tests covering the AmpioClient connection lifecycle.

These tests inject a scripted FakeBroker through the ``mqtt_client_factory``
transport seam so the connect/subscribe/publish/messages path can be
exercised without a real broker. They cover:
- the ``AmpioClient.test_connection`` config-flow helper,
- ``request_*`` raising when disconnected,
- ``stop()`` cancelling the runner cleanly,
- ``start()`` driving a successful discovery via scripted broker messages,
- a runtime credential rejection reaching the auth-failure listener while a
  transient outage does not.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiomqtt
import pytest
from conftest import (
    DATA_DEVICES_TOPIC,
    DETAILS_TOPIC,
    DEVICES_TOPIC,
    INFO_TOPIC,
    PARAMS_DEVICES_TOPIC,
    STATES_TOPIC,
    USER,
    FakeBroker,
    Message,
)
from paho.mqtt.enums import MQTTErrorCode
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from ampio_mqtt import (
    AccessTier,
    AmpioClient,
    AmpioConnectionError,
    AmpioTimeoutError,
    AuthFailed,
    AvailabilityChanged,
    ConnectionDied,
)
from ampio_mqtt._connection import _is_auth_error
from ampio_mqtt.endpoints import ENDPOINTS, request_topic
from ampio_mqtt.errors import AmpioAuthError


def _auth_rejection(name: str = "Not authorized") -> aiomqtt.MqttCodeError:
    """A CONNACK rejection the way aiomqtt >= 2.2 raises it: a coded error
    carrying the v5 ReasonCode paho's VERSION2 callbacks normalize to."""
    return aiomqtt.MqttCodeError(ReasonCode(PacketTypes.CONNACK, name))


# --- AmpioClient.test_connection ------------------------------------------


async def test_connection_returns_server_info_on_happy_path() -> None:
    """Messages on other topics are skipped until the info topic arrives."""
    broker = FakeBroker()
    broker.scripted_messages = [
        Message("unrelated/topic", b"junk"),
        Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 42, "userId": "-1"}}).encode()
        ),
    ]
    info = await AmpioClient.test_connection(
        "h", 1883, USER, "p", info_timeout=1, mqtt_client_factory=broker.factory
    )
    assert info.mac == 42
    assert info.access_tier is AccessTier.ADMIN
    assert INFO_TOPIC in broker.subscribed
    # The M-SERV publishes at QoS 1; a QoS 0 subscription would let the
    # broker downgrade its delivery leg to at-most-once (#65).
    assert broker.subscribed_qos == [1]
    # The info request publish was sent with an empty body, acknowledged by
    # the broker (QoS 1, #68).
    assert (f"ampio/control/{USER}/info", b"") in broker.published
    assert broker.published_qos == [1]


async def test_connection_reports_a_restricted_account_before_setup() -> None:
    """A config flow can reject a non-admin account at validation time (#59).

    An app-created user carries its positive users-table row id; only the
    reserved `admin` login reports the pseudo-user -1.
    """
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 42, "userId": "4"}}).encode()
        )
    ]
    info = await AmpioClient.test_connection(
        "h", 1883, USER, "p", info_timeout=1, mqtt_client_factory=broker.factory
    )
    assert info.user_id == 4
    assert info.access_tier is AccessTier.RESTRICTED


async def test_connection_raises_timeout_when_info_never_arrives() -> None:
    """A broker that connects but never replies raises AmpioTimeoutError.

    The timeout error subclasses AmpioConnectionError, so a consumer that
    lumps all connection problems together keeps working while one that wants
    "try again" semantics can catch the subclass first (#54).
    """
    broker = FakeBroker()
    with pytest.raises(AmpioTimeoutError):
        await AmpioClient.test_connection(
            "h", 1883, USER, "p", info_timeout=0.1, mqtt_client_factory=broker.factory
        )
    assert issubclass(AmpioTimeoutError, AmpioConnectionError)


async def test_connection_returns_info_without_identity_as_is() -> None:
    """A reply that arrives without identity fields returns, not raises (#54)."""
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(INFO_TOPIC, json.dumps({"Results": {}}).encode())
    ]
    info = await AmpioClient.test_connection(
        "h", 1883, USER, "p", info_timeout=1, mqtt_client_factory=broker.factory
    )
    assert info.mac is None
    assert info.server_version is None
    assert info.access_tier is AccessTier.UNKNOWN


async def test_connection_raises_auth_error_on_bad_credentials() -> None:
    broker = FakeBroker()
    broker.enter_errors = [_auth_rejection()]
    with pytest.raises(AmpioAuthError):
        await AmpioClient.test_connection(
            "h", 1883, USER, "bad", info_timeout=0.1, mqtt_client_factory=broker.factory
        )


async def test_connection_raises_connection_error_on_transport_failure() -> None:
    broker = FakeBroker()
    broker.enter_errors = [aiomqtt.MqttError("Connection refused")]
    with pytest.raises(AmpioConnectionError):
        await AmpioClient.test_connection(
            "h", 1883, USER, "p", info_timeout=0.1, mqtt_client_factory=broker.factory
        )


# --- discovery requests when disconnected ---------------------------------


async def test_refresh_raises_when_disconnected() -> None:
    client = AmpioClient("h", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.refresh()


async def test_refresh_skips_admin_requests_on_a_restricted_tier() -> None:
    """Once the info reply identifies a RESTRICTED account, refresh() stops
    publishing the config requests the M-SERV never answers for that tier;
    the login's admin bit cannot change mid-session."""
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 99, "userId": "4"}}).encode()
        ),
    ]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        # The first refresh ran on an UNKNOWN tier and asked for everything.
        assert len(broker.published) == sum(1 for ep in ENDPOINTS if ep.initial)
        broker.published.clear()
        await client.refresh()
        assert sorted(p for _t, p in broker.published) == [
            b"",  # info
            b"",  # states
            b"devices",  # data catalogue
            b"params_devices",
        ]
        assert all(
            t.endswith(("/data", "/states", "/info")) for t, _p in broker.published
        )
    finally:
        await client.stop()


async def test_refresh_skips_data_requests_on_the_admin_tier() -> None:
    """Once the info reply identifies the ADMIN account, refresh() stops
    publishing the app-sync pair, which only repeats what the config
    catalogue already carries."""
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 99, "userId": "-1"}}).encode()
        ),
    ]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        # The first refresh ran on an UNKNOWN tier and asked for everything.
        assert len(broker.published) == sum(1 for ep in ENDPOINTS if ep.initial)
        broker.published.clear()
        await client.refresh()
        assert sorted(p for _t, p in broker.published) == [
            b"",  # info
            b"",  # states
            b"devices",  # module list
            b"devicesDetails",
        ]
        assert all(
            t.endswith(("/config", "/states", "/info")) for t, _p in broker.published
        )
    finally:
        await client.stop()


# --- stop() and start() lifecycle -----------------------------------------


async def test_stop_cancels_pending_runner() -> None:
    """stop() cancels a runner that is sleeping in the reconnect backoff."""

    async def _sleep_forever(*_: object) -> None:
        await asyncio.sleep(3600)

    client = AmpioClient("h", username=USER, reconnect_interval=3600)
    client._connection._runner = asyncio.create_task(_sleep_forever())
    await client.stop()
    assert client._connection._runner is None


async def test_second_start_recycles_the_connection_loop() -> None:
    """start() on a running client closes the previous loop first - two
    loops would share one client id and steal the session from each other
    on every reconnect."""
    broker = FakeBroker()
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.01)
    first_runner = client._connection._runner
    assert first_runner is not None
    await client.start(timeout=2.0, discovery_timeout=0.01)
    try:
        assert first_runner.done()
        assert client.available is True
    finally:
        await client.stop()
    assert client._connection._runner is None


async def test_start_drives_full_discovery_through_mocked_broker() -> None:
    """A scripted broker drives start() through connect + discovery to completion."""
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(DEVICES_TOPIC, json.dumps({"List": []}).encode()),
        Message(DETAILS_TOPIC, json.dumps({"List": []}).encode()),
        Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
        Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 99, "userId": "-1"}}).encode()
        ),
    ]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    completed = await client.start(timeout=2.0, discovery_timeout=1.0)
    try:
        assert completed is True
        assert client.available is True
        assert client.server_info is not None and client.server_info.mac == 99
        # The five expected subscriptions were issued.
        assert {
            DETAILS_TOPIC,
            DEVICES_TOPIC,
            STATES_TOPIC,
            INFO_TOPIC,
            f"ampio/fromDB/{USER}/ob/+/state",
        }.issubset(set(broker.subscribed))
        # Every runtime subscription asks for QoS 1 (#65), and every
        # discovery request publish goes out at QoS 1 (#68).
        assert set(broker.subscribed_qos) == {1}
        assert set(broker.published_qos) == {1}
        # start() publishes exactly the initial-endpoint request set, once.
        assert set(broker.published) == {
            (request_topic(ep, USER), ep.req_payload.encode())
            for ep in ENDPOINTS
            if ep.initial
        }
        assert len(broker.published) == sum(1 for ep in ENDPOINTS if ep.initial)
    finally:
        await client.stop()


async def test_wait_for_initial_discovery_returns_true_when_all_arrive() -> None:
    """All four discovery messages populate the client and the wait returns True."""
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(
            DEVICES_TOPIC,
            json.dumps(
                {"List": [{"id": 17, "mac": 52111, "typ_urzadzenia": 44}]}
            ).encode(),
        ),
        Message(
            DETAILS_TOPIC,
            json.dumps(
                {
                    "List": [
                        {
                            "id": 41,
                            "id_urzadzenia": 17,
                            "typ_komponentu": "temp",
                            "interpretacja": 1,
                            "opis_menu": "Salon",
                        }
                    ]
                }
            ).encode(),
        ),
        Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
        Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 99, "userId": "-1"}}).encode()
        ),
    ]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=1.0)
    try:
        assert await client.wait_for_initial_discovery(timeout=1.0) is True
        assert client.access_tier is AccessTier.ADMIN
        assert 17 in client.modules
        assert 41 in client.objects
        assert client.server_info is not None and client.server_info.mac == 99
        # The signals latch: a repeat call returns True immediately, and a
        # reconnect (whose refresh replays the scripted set) keeps it True.
        assert await client.wait_for_initial_discovery(timeout=0.01) is True
        broker.stream_error = aiomqtt.MqttError("connection lost")
        await asyncio.sleep(0.05)
        broker.stream_error = None
        assert await client.wait_for_initial_discovery(timeout=0.01) is True
    finally:
        await client.stop()


async def test_restricted_account_completes_via_data_surface_fallback() -> None:
    """With the config surface silent, the app-sync pair completes discovery.

    This is the non-admin shape: `config/devicesDetails` and
    `config/devices` never answer, while `data/devices` (grant-filtered, with
    full metadata) and `data/params_devices` do.
    """
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(
            DATA_DEVICES_TOPIC,
            json.dumps(
                {
                    "List": [
                        {
                            "id": 24,
                            "id_urzadzenia": 20,
                            "typ_komponentu": "lin_wej",
                            "interpretacja": 7,
                            "funkcja": 5,
                            "leafId": "0_cb9b_75_0_0",
                            "opis_menu": "CO2",
                        }
                    ]
                }
            ).encode(),
        ),
        Message(
            PARAMS_DEVICES_TOPIC,
            json.dumps({"List": [{"id": 24, "params": 1}]}).encode(),
        ),
        Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
        Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 99, "userId": "4"}}).encode()
        ),
    ]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=1.0)
    try:
        assert await client.wait_for_initial_discovery(timeout=1.0) is True
        assert client.access_tier is AccessTier.RESTRICTED
        obj = client.objects[24]
        assert obj.name == "CO2"
        assert obj.kind is not None and obj.kind.device_class == "carbon_dioxide"
        assert obj.stable_key == "leaf_0_cb9b_75_0_0"
        assert obj.visible is True
        assert client.modules == {}
        assert client.server_info is not None and client.server_info.mac == 99
    finally:
        await client.stop()


async def test_unknown_tier_completes_discovery_via_the_data_pair() -> None:
    """An info reply that carries no account id leaves the tier UNKNOWN, and
    discovery still completes through the app-sync data pair - the documented
    fallback, since that pair answers for every account."""
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
        Message(INFO_TOPIC, json.dumps({"Results": {"mac": 99}}).encode()),
        Message(DATA_DEVICES_TOPIC, json.dumps({"List": []}).encode()),
        Message(PARAMS_DEVICES_TOPIC, json.dumps({"List": []}).encode()),
    ]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    completed = await client.start(timeout=2.0, discovery_timeout=1.0)
    try:
        assert completed is True
        assert client.access_tier is AccessTier.UNKNOWN
    finally:
        await client.stop()


async def test_runtime_auth_rejection_fires_listener_and_stops() -> None:
    """A credential rejection on reconnect reaches the auth-failure listener.

    Sequence: connect fine, the broker drops the link, the reconnect attempt
    is rejected as unauthorized - the shape a credential change on the broker
    produces. The consumer must learn the loop stopped for good rather than
    seeing only the availability drop a transient outage also produces (#53).
    """
    broker = FakeBroker()
    broker.stream_error = aiomqtt.MqttError("connection lost")
    broker.enter_errors = [None, _auth_rejection()]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    availability: list[bool] = []
    failures: list[str] = []
    client.subscribe(lambda e: availability.append(e.available), of=AvailabilityChanged)
    client.subscribe(lambda e: failures.append(e.reason), of=AuthFailed)
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        async with asyncio.timeout(2.0):
            while client.auth_failure is None:
                await asyncio.sleep(0.01)
    finally:
        await client.stop()
    assert len(failures) == 1 and "authorized" in failures[0].lower()
    assert client.auth_failure == failures[0]
    assert availability == [True, False]


async def test_fresh_start_clears_a_runtime_auth_failure() -> None:
    """auth_failure is terminal for one run, not for the client: a new
    start() - presumably with accepted credentials - clears it and the
    connection comes back up."""
    broker = FakeBroker()
    broker.stream_error = aiomqtt.MqttError("connection lost")
    broker.enter_errors = [None, _auth_rejection()]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.05)
    async with asyncio.timeout(2.0):
        while client.auth_failure is None:
            await asyncio.sleep(0.01)
        runner = client._connection._runner
        assert runner is not None
        await runner  # the loop has stopped for good

    # The broker accepts the credentials again.
    broker.enter_errors = []
    broker.stream_error = None
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        assert client.auth_failure is None
        assert client.available is True
    finally:
        await client.stop()


async def test_initial_auth_rejection_raises_without_firing_listener() -> None:
    """A rejection during start() raises AmpioAuthError; the listener is for
    the runtime path only, so a config flow does not get a double signal."""
    broker = FakeBroker()
    broker.enter_errors = [_auth_rejection()]
    client = AmpioClient("h", username=USER, mqtt_client_factory=broker.factory)
    failures: list[str] = []
    client.subscribe(lambda e: failures.append(e.reason), of=AuthFailed)
    with pytest.raises(AmpioAuthError):
        await client.start(timeout=2.0, discovery_timeout=0.05)
    assert failures == []
    assert client.auth_failure is not None
    assert "authorized" in client.auth_failure.lower()
    assert client._connection._runner is None  # stop() ran during the raise


async def test_transient_outage_leaves_auth_failure_unset() -> None:
    """An outage with recovery keeps auth_failure None while the loop retries."""
    broker = FakeBroker()
    broker.stream_error = aiomqtt.MqttError("connection lost")
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    availability: list[bool] = []
    client.subscribe(lambda e: availability.append(e.available), of=AvailabilityChanged)
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        async with asyncio.timeout(2.0):
            while availability.count(True) < 2:
                await asyncio.sleep(0.01)
        assert client.auth_failure is None
    finally:
        await client.stop()


async def test_loop_crash_dispatches_connection_died_and_stops() -> None:
    """An unexpected exception is terminal: availability drops first, then
    ConnectionDied, and nothing retries - the broker being fine is exactly
    what made the dead loop indistinguishable from an outage before."""
    broker = FakeBroker()
    broker.stream_error = RuntimeError("injected bug")
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    order: list[object] = []
    client.subscribe(order.append, of=(AvailabilityChanged, ConnectionDied))
    await client.start(timeout=2.0, discovery_timeout=0.05)
    await asyncio.sleep(0.3)  # several reconnect intervals
    try:
        assert order == [
            AvailabilityChanged(True),
            AvailabilityChanged(False),
            ConnectionDied("Connection loop died: injected bug"),
        ]
        assert client.available is False
        assert client.stats.reconnect_count == 0
        assert client.stats.last_error == "injected bug"
    finally:
        await client.stop()


async def test_crash_during_start_raises_connection_error() -> None:
    """A loop crash before the first connect surfaces from start() itself,
    promptly, and dispatches nothing - mirroring the auth path."""
    broker = FakeBroker()
    broker.enter_errors = [RuntimeError("boom at connect")]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    events: list[object] = []
    client.subscribe(events.append)
    with pytest.raises(AmpioConnectionError, match="Connection loop died"):
        await client.start(timeout=5.0, discovery_timeout=0.05)
    assert events == []


async def test_start_reports_discovery_timeout() -> None:
    """start() returns False when discovery does not complete in time; the
    connection stays up and discovery continues opportunistically."""
    broker = FakeBroker()
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    completed = await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        assert completed is False
        assert client.available is True
    finally:
        await client.stop()


async def test_publish_failure_during_refresh_recycles_the_session() -> None:
    """A broker failure inside the on-connect refresh reconnects instead of
    killing the loop: publish() wraps aiomqtt errors, and the runner treats
    the wrapped form like any transport drop."""
    broker = FakeBroker()
    broker.publish_errors = [aiomqtt.MqttError("broken pipe")]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        async with asyncio.timeout(2.0):
            while not (client.available and client.stats.reconnect_count >= 1):
                await asyncio.sleep(0.01)
        assert client.stats.last_error == "broken pipe"
        assert client.auth_failure is None
    finally:
        await client.stop()


async def test_unacknowledged_publish_raises_timeout(
    monkeypatch: pytest.MonkeyPatch,
    connected: tuple[AmpioClient, FakeBroker],
) -> None:
    """A PUBACK that never arrives surfaces as the retryable AmpioTimeoutError."""
    client, broker = connected

    async def _hang(topic: str, payload: bytes = b"", qos: int = 0) -> None:
        await asyncio.sleep(3600)

    broker.publish = _hang  # type: ignore[method-assign]
    monkeypatch.setattr("ampio_mqtt._connection._PUBLISH_TIMEOUT", 0.05)
    with pytest.raises(AmpioTimeoutError):
        await client.send_event(9)


async def test_consumer_stop_is_not_an_availability_event() -> None:
    """stop() must not report the drop it causes itself (#56).

    Every consumer reacting to availability otherwise sees a deliberate
    shutdown as a lost connection; the HA integration carried a
    shutting-down flag purely to suppress that false transition.
    """
    broker = FakeBroker()
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    availability: list[bool] = []
    client.subscribe(lambda e: availability.append(e.available), of=AvailabilityChanged)
    await client.start(timeout=2.0, discovery_timeout=0.05)
    assert availability == [True]
    await client.stop()
    assert availability == [True]
    assert client.available is False


async def test_availability_notifies_again_after_restart() -> None:
    """A stop() suppression must not leak into the next start()."""
    broker = FakeBroker()
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    availability: list[bool] = []
    client.subscribe(lambda e: availability.append(e.available), of=AvailabilityChanged)
    await client.start(timeout=2.0, discovery_timeout=0.05)
    await client.stop()
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        assert availability == [True, True]
    finally:
        await client.stop()


async def test_wait_for_initial_discovery_returns_false_on_timeout() -> None:
    """A partial discovery set leaves the wait returning False without raising."""
    # No info message scripted -> the info endpoint's reply channel never
    # latches, so the discovery wait cannot complete.
    broker = FakeBroker()
    broker.scripted_messages = [
        Message(DEVICES_TOPIC, json.dumps({"List": []}).encode()),
        Message(DETAILS_TOPIC, json.dumps({"List": []}).encode()),
        Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
    ]
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.0, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.1)
    try:
        assert await client.wait_for_initial_discovery(timeout=0.1) is False
    finally:
        await client.stop()


# --- subscription verdicts -------------------------------------------------


async def test_rejected_subscriptions_are_recorded_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A SUBACK failure code lands in the stats while the connection stays
    up, and nothing reaches the warning level: a denied raw-tree topic is
    the designed state for a standard account, so the consumer judges the
    verdicts from `subscribe_failures` instead of the log."""
    denied = "ampio/from/+/state/f/+"
    broker = FakeBroker()
    broker.suback_codes = {denied: 0x87}
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._connection"):
        await client.start(timeout=2.0, discovery_timeout=0.05)
        try:
            assert client.available is True
            assert client.stats.subscribe_failures == {denied: 0x87}
        finally:
            await client.stop()
    assert not any(denied in r.getMessage() for r in caplog.records)


async def test_granted_subscriptions_leave_no_failures() -> None:
    broker = FakeBroker()
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    await client.start(timeout=2.0, discovery_timeout=0.05)
    try:
        assert client.stats.subscribe_failures == {}
    finally:
        await client.stop()


# --- auth-failure classification ------------------------------------------


@pytest.mark.parametrize("name", ["Not authorized", "Bad user name or password"])
def test_is_auth_error_matches_the_v5_reason_codes(name: str) -> None:
    assert _is_auth_error(_auth_rejection(name))


def test_is_auth_error_accepts_a_plain_int_code() -> None:
    assert _is_auth_error(aiomqtt.MqttCodeError(135, "rejected"))


def test_is_auth_error_walks_the_cause_chain() -> None:
    """The mid-iteration drop shape: aiomqtt raises a bare MqttError with the
    coded disconnect error attached as its ``__cause__``. The error text alone
    carries no code, so only the chain walk can classify it."""
    outer = aiomqtt.MqttError("Disconnected during message iteration")
    outer.__cause__ = aiomqtt.MqttCodeError(
        ReasonCode(PacketTypes.DISCONNECT, "Not authorized"),
        "Unexpected disconnection",
    )
    assert _is_auth_error(outer)


def test_is_auth_error_rejects_transport_failures() -> None:
    # MQTTErrorCode.MQTT_ERR_CONN_REFUSED is 5 - an auth code in raw MQTT
    # 3.1.1 CONNACK numbering, a plain transport failure in paho's own enum.
    # Matching only the v5 codes keeps the two namespaces apart.
    assert not _is_auth_error(
        aiomqtt.MqttCodeError(MQTTErrorCode.MQTT_ERR_CONN_REFUSED)
    )
    assert not _is_auth_error(aiomqtt.MqttCodeError(None, "no code at all"))
    assert not _is_auth_error(aiomqtt.MqttError("Not authorized"))
    assert not _is_auth_error(
        aiomqtt.MqttCodeError(ReasonCode(PacketTypes.CONNACK, "Server unavailable"))
    )


def test_auth_error_is_not_a_connection_error() -> None:
    """A config flow catching AmpioConnectionError broadly must not swallow
    the credential rejection that needs a different user action."""
    assert not issubclass(AmpioAuthError, AmpioConnectionError)


async def test_a_rejected_namespace_filter_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the raw tree is expected to be denied; a rejected fromDB filter
    means a broken broker or ACL and must be loud."""
    denied = DETAILS_TOPIC
    broker = FakeBroker()
    broker.suback_codes = {denied: 0x87}
    client = AmpioClient(
        "h", username=USER, reconnect_interval=0.05, mqtt_client_factory=broker.factory
    )
    with caplog.at_level(logging.WARNING, logger="ampio_mqtt._connection"):
        await client.start(timeout=2.0, discovery_timeout=0.05)
        try:
            assert client.stats.subscribe_failures == {denied: 0x87}
        finally:
            await client.stop()
    assert any(denied in r.getMessage() for r in caplog.records)
