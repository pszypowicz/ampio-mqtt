"""Tests covering the AmpioClient connection lifecycle.

These tests mock `aiomqtt.Client` so the connect/subscribe/publish/messages
path can be exercised without a real broker. They cover:
- the ``AmpioClient.test_connection`` config-flow helper,
- ``request_*`` raising when disconnected,
- ``stop()`` cancelling the runner cleanly,
- ``start()`` driving a successful discovery via mocked broker messages,
- a runtime credential rejection reaching the auth-failure listener while a
  transient outage does not.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar, Self
from unittest.mock import patch

import aiomqtt
import pytest

from ampio_mqtt import AccessTier, AmpioClient, AmpioConnectionError, AmpioTimeoutError
from ampio_mqtt.errors import AmpioAuthError

USER = "u"

# Discovery response topics the M-SERV publishes after the auto-discovery
# keywords are sent; shared by the start()/discovery lifecycle tests.
DETAILS_TOPIC = f"ampio/fromDB/{USER}/config/devicesDetails"
DEVICES_TOPIC = f"ampio/fromDB/{USER}/config/devices"
STATES_TOPIC = f"ampio/fromDB/{USER}/data/states"
INFO_TOPIC = f"ampio/fromDB/{USER}/data/info"
DATA_DEVICES_TOPIC = f"ampio/fromDB/{USER}/data/devices"
PARAMS_DEVICES_TOPIC = f"ampio/fromDB/{USER}/data/params_devices"


class _Message:
    """Minimal stand-in for `aiomqtt.Message`."""

    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class FakeMqttClient:
    """Configurable async-context-manager replacement for `aiomqtt.Client`.

    Instances are constructed via class-level state because aiomqtt.Client is
    patched at import time and the AmpioClient constructs it directly.
    """

    enter_error: BaseException | None = None
    # Per-attempt connect outcomes, consumed left to right; None connects
    # fine. Lets a test script "first connect works, the reconnect is
    # rejected". Falls back to `enter_error` once exhausted.
    enter_errors: ClassVar[list[BaseException | None]] = []
    enter_delay: float = 0.0
    # Raised from the message stream once the scripted messages are consumed,
    # simulating the broker dropping an established connection.
    stream_error: BaseException | None = None
    scripted_messages: ClassVar[list[_Message]] = []
    published: ClassVar[list[tuple[str, bytes]]] = []
    subscribed: ClassVar[list[str]] = []

    @classmethod
    def reset(cls) -> None:
        cls.enter_error = None
        cls.enter_errors = []
        cls.enter_delay = 0.0
        cls.stream_error = None
        cls.scripted_messages = []
        cls.published = []
        cls.subscribed = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._messages_queue: asyncio.Queue[_Message] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        if self.enter_delay:
            await asyncio.sleep(self.enter_delay)
        error = (
            FakeMqttClient.enter_errors.pop(0)
            if FakeMqttClient.enter_errors
            else self.enter_error
        )
        if error is not None:
            raise error
        for msg in self.scripted_messages:
            await self._messages_queue.put(msg)
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def subscribe(self, topic: str) -> None:
        FakeMqttClient.subscribed.append(topic)

    async def publish(self, topic: str, payload: bytes = b"") -> None:
        FakeMqttClient.published.append((topic, payload))

    @property
    def messages(self) -> FakeMqttClient:
        return self

    def __aiter__(self) -> FakeMqttClient:
        return self

    async def __anext__(self) -> _Message:
        if FakeMqttClient.stream_error is not None and self._messages_queue.empty():
            raise FakeMqttClient.stream_error
        try:
            return await asyncio.wait_for(self._messages_queue.get(), timeout=5.0)
        except TimeoutError as err:
            raise StopAsyncIteration from err


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeMqttClient.reset()


# --- AmpioClient.test_connection ------------------------------------------


async def test_connection_returns_server_info_on_happy_path() -> None:
    info_topic = f"ampio/fromDB/{USER}/data/info"
    FakeMqttClient.scripted_messages = [
        _Message(
            info_topic, json.dumps({"Results": {"mac": 42, "userId": "-1"}}).encode()
        )
    ]
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        info = await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=1)
    assert info.mac == 42
    assert info.access_tier is AccessTier.ADMIN
    assert info_topic in FakeMqttClient.subscribed
    # The info request publish was sent with an empty body.
    assert (f"ampio/control/{USER}/info", b"") in FakeMqttClient.published


async def test_connection_reports_a_restricted_account_before_setup() -> None:
    """A config flow can reject a non-admin account at validation time (#59).

    An app-created user carries its positive users-table row id; only the
    reserved `admin` login reports the pseudo-user -1.
    """
    info_topic = f"ampio/fromDB/{USER}/data/info"
    FakeMqttClient.scripted_messages = [
        _Message(
            info_topic, json.dumps({"Results": {"mac": 42, "userId": "4"}}).encode()
        )
    ]
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        info = await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=1)
    assert info.user_id == 4
    assert info.access_tier is AccessTier.RESTRICTED


async def test_connection_raises_timeout_when_info_never_arrives() -> None:
    """A broker that connects but never replies raises AmpioTimeoutError.

    The timeout error subclasses AmpioConnectionError, so a consumer that
    lumps all connection problems together keeps working while one that wants
    "try again" semantics can catch the subclass first (#54).
    """
    with (
        patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient),
        pytest.raises(AmpioTimeoutError),
    ):
        await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=0.1)
    assert issubclass(AmpioTimeoutError, AmpioConnectionError)


async def test_connection_returns_info_without_identity_as_is() -> None:
    """A reply that arrives without identity fields returns, not raises (#54)."""
    info_topic = f"ampio/fromDB/{USER}/data/info"
    FakeMqttClient.scripted_messages = [
        _Message(info_topic, json.dumps({"Results": {}}).encode())
    ]
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        info = await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=1)
    assert info.mac is None
    assert info.server_version is None
    assert info.access_tier is AccessTier.UNKNOWN


async def test_connection_raises_auth_error_on_bad_credentials() -> None:
    FakeMqttClient.enter_error = aiomqtt.MqttError("Not authorized")
    with (
        patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient),
        pytest.raises(AmpioAuthError),
    ):
        await AmpioClient.test_connection("h", 1883, USER, "bad", info_timeout=0.1)


async def test_connection_raises_connection_error_on_transport_failure() -> None:
    FakeMqttClient.enter_error = aiomqtt.MqttError("Connection refused")
    with (
        patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient),
        pytest.raises(AmpioConnectionError),
    ):
        await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=0.1)


async def test_connection_ignores_unrelated_topics() -> None:
    """Messages on other topics are skipped until the info topic arrives."""
    info_topic = f"ampio/fromDB/{USER}/data/info"
    FakeMqttClient.scripted_messages = [
        _Message("unrelated/topic", b"junk"),
        _Message(info_topic, json.dumps({"Results": {"mac": 7}}).encode()),
    ]
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        info = await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=1)
    assert info.mac == 7


# --- request_* and _publish_config when disconnected ----------------------


@pytest.mark.parametrize("name", ["details", "devices", "states", "info"])
async def test_request_raises_when_disconnected(name: str) -> None:
    client = AmpioClient("h", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.request(name)


async def test_refresh_raises_when_disconnected() -> None:
    client = AmpioClient("h", username=USER)
    with pytest.raises(AmpioConnectionError):
        await client.refresh()


# --- stop() and start() lifecycle -----------------------------------------


async def test_stop_cancels_pending_runner() -> None:
    """stop() cancels a runner that is sleeping in the reconnect backoff."""

    async def _sleep_forever(*_: object) -> None:
        await asyncio.sleep(3600)

    client = AmpioClient("h", username=USER, reconnect_interval=3600)
    client._connection._runner = asyncio.create_task(_sleep_forever())
    await client.stop()
    assert client._connection._runner is None


async def test_start_drives_full_discovery_through_mocked_broker() -> None:
    """A scripted broker drives start() through connect + discovery to completion."""
    FakeMqttClient.scripted_messages = [
        _Message(DEVICES_TOPIC, json.dumps({"List": []}).encode()),
        _Message(DETAILS_TOPIC, json.dumps({"List": []}).encode()),
        _Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
        _Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 99, "userId": "-1"}}).encode()
        ),
    ]
    client = AmpioClient("h", username=USER, reconnect_interval=0.0)
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=1.0)
    try:
        assert client.available is True
        assert client.server_info is not None and client.server_info.mac == 99
        # The five expected subscriptions were issued.
        assert {
            DETAILS_TOPIC,
            DEVICES_TOPIC,
            STATES_TOPIC,
            INFO_TOPIC,
            f"ampio/fromDB/{USER}/ob/+/state",
        }.issubset(set(FakeMqttClient.subscribed))
    finally:
        await client.stop()


async def test_wait_for_initial_discovery_returns_true_when_all_arrive() -> None:
    """All four discovery messages populate the client and the wait returns True."""
    FakeMqttClient.scripted_messages = [
        _Message(
            DEVICES_TOPIC,
            json.dumps(
                {"List": [{"id": 17, "mac": 52111, "typ_urzadzenia": 44}]}
            ).encode(),
        ),
        _Message(
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
        _Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
        _Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 99, "userId": "-1"}}).encode()
        ),
    ]
    client = AmpioClient("h", username=USER, reconnect_interval=0.0)
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=1.0)
    try:
        assert await client.wait_for_initial_discovery(timeout=1.0) is True
        assert client.access_tier is AccessTier.ADMIN
        assert 17 in client.modules
        assert 41 in client.objects
        assert client.server_info is not None and client.server_info.mac == 99
    finally:
        await client.stop()


async def test_restricted_account_completes_via_data_surface_fallback() -> None:
    """With the config surface silent, the app-sync pair completes discovery.

    This is the non-admin shape verified live: `config/devicesDetails` and
    `config/devices` never answer, while `data/devices` (grant-filtered, with
    full metadata) and `data/params_devices` do.
    """
    FakeMqttClient.scripted_messages = [
        _Message(
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
        _Message(
            PARAMS_DEVICES_TOPIC,
            json.dumps({"List": [{"id": 24, "params": 1}]}).encode(),
        ),
        _Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
        _Message(
            INFO_TOPIC, json.dumps({"Results": {"mac": 99, "userId": "4"}}).encode()
        ),
    ]
    client = AmpioClient("h", username=USER, reconnect_interval=0.0)
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
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


async def test_runtime_auth_rejection_fires_listener_and_stops() -> None:
    """A credential rejection on reconnect reaches the auth-failure listener.

    Sequence: connect fine, the broker drops the link, the reconnect attempt
    is rejected as unauthorized - the shape a credential change on the broker
    produces. The consumer must learn the loop stopped for good rather than
    seeing only the availability drop a transient outage also produces (#53).
    """
    FakeMqttClient.stream_error = aiomqtt.MqttError("connection lost")
    FakeMqttClient.enter_errors = [None, aiomqtt.MqttError("[code:135] Not authorized")]
    client = AmpioClient("h", username=USER, reconnect_interval=0.05)
    availability: list[bool] = []
    failures: list[str] = []
    client.add_availability_listener(availability.append)
    client.add_auth_failure_listener(failures.append)
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=0.05)
        try:
            async with asyncio.timeout(2.0):
                while client.auth_failure is None:
                    await asyncio.sleep(0.01)
        finally:
            await client.stop()
    assert failures == ["[code:135] Not authorized"]
    assert client.auth_failure == "[code:135] Not authorized"
    assert availability == [True, False]


async def test_initial_auth_rejection_raises_without_firing_listener() -> None:
    """A rejection during start() raises AmpioAuthError; the listener is for
    the runtime path only, so a config flow does not get a double signal."""
    FakeMqttClient.enter_error = aiomqtt.MqttError("Not authorized")
    client = AmpioClient("h", username=USER)
    failures: list[str] = []
    client.add_auth_failure_listener(failures.append)
    with (
        patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient),
        pytest.raises(AmpioAuthError),
    ):
        await client.start(timeout=2.0, discovery_timeout=0.05)
    assert failures == []
    assert client.auth_failure == "Not authorized"


async def test_transient_outage_leaves_auth_failure_unset() -> None:
    """An outage with recovery keeps auth_failure None while the loop retries."""
    FakeMqttClient.stream_error = aiomqtt.MqttError("connection lost")
    client = AmpioClient("h", username=USER, reconnect_interval=0.05)
    availability: list[bool] = []
    client.add_availability_listener(availability.append)
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=0.05)
        try:
            async with asyncio.timeout(2.0):
                while availability.count(True) < 2:
                    await asyncio.sleep(0.01)
            assert client.auth_failure is None
        finally:
            await client.stop()


async def test_consumer_stop_is_not_an_availability_event() -> None:
    """stop() must not report the drop it causes itself (#56).

    Every consumer reacting to availability otherwise sees a deliberate
    shutdown as a lost connection; the HA integration carried a
    shutting-down flag purely to suppress that false transition.
    """
    client = AmpioClient("h", username=USER, reconnect_interval=0.05)
    availability: list[bool] = []
    client.add_availability_listener(availability.append)
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=0.05)
        assert availability == [True]
        await client.stop()
    assert availability == [True]
    assert client.available is False


async def test_availability_notifies_again_after_restart() -> None:
    """A stop() suppression must not leak into the next start()."""
    client = AmpioClient("h", username=USER, reconnect_interval=0.05)
    availability: list[bool] = []
    client.add_availability_listener(availability.append)
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=0.05)
        await client.stop()
        await client.start(timeout=2.0, discovery_timeout=0.05)
        try:
            assert availability == [True, True]
        finally:
            await client.stop()


async def test_wait_for_initial_discovery_returns_false_on_timeout() -> None:
    """A partial discovery set leaves the wait returning False without raising."""
    # No info message scripted -> _info_received never fires.
    FakeMqttClient.scripted_messages = [
        _Message(DEVICES_TOPIC, json.dumps({"List": []}).encode()),
        _Message(DETAILS_TOPIC, json.dumps({"List": []}).encode()),
        _Message(STATES_TOPIC, json.dumps({"List": []}).encode()),
    ]
    client = AmpioClient("h", username=USER, reconnect_interval=0.0)
    with patch("ampio_mqtt._connection.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=0.1)
        try:
            assert await client.wait_for_initial_discovery(timeout=0.1) is False
        finally:
            await client.stop()
