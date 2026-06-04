"""Tests covering the AmpioClient connection lifecycle.

These tests mock `aiomqtt.Client` so the connect/subscribe/publish/messages
path can be exercised without a real broker. They cover:
- the ``AmpioClient.test_connection`` config-flow helper,
- ``request_*`` raising when disconnected,
- ``stop()`` cancelling the runner cleanly,
- ``start()`` driving a successful discovery via mocked broker messages.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import aiomqtt
import pytest

from ampio_mqtt import AmpioClient, AmpioConnectionError
from ampio_mqtt.errors import AmpioAuthError

USER = "u"


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
    enter_delay: float = 0.0
    scripted_messages: list[_Message] = []
    published: list[tuple[str, bytes]] = []
    subscribed: list[str] = []

    @classmethod
    def reset(cls) -> None:
        cls.enter_error = None
        cls.enter_delay = 0.0
        cls.scripted_messages = []
        cls.published = []
        cls.subscribed = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._messages_queue: asyncio.Queue[_Message] = asyncio.Queue()

    async def __aenter__(self) -> FakeMqttClient:
        if self.enter_delay:
            await asyncio.sleep(self.enter_delay)
        if self.enter_error is not None:
            raise self.enter_error
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
        _Message(info_topic, json.dumps({"Results": {"mac": 42}}).encode())
    ]
    with patch("ampio_mqtt.client.aiomqtt.Client", FakeMqttClient):
        info = await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=1)
    assert info.mac == 42
    assert info_topic in FakeMqttClient.subscribed
    # The info request publish was sent with an empty body.
    assert (f"ampio/control/{USER}/info", b"") in FakeMqttClient.published


async def test_connection_returns_empty_on_info_timeout() -> None:
    """A broker that connects but never replies returns an empty AmpioServerInfo."""
    with patch("ampio_mqtt.client.aiomqtt.Client", FakeMqttClient):
        info = await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=0.1)
    assert info.mac is None
    assert info.server_version is None


async def test_connection_raises_auth_error_on_bad_credentials() -> None:
    FakeMqttClient.enter_error = aiomqtt.MqttError("Not authorized")
    with (
        patch("ampio_mqtt.client.aiomqtt.Client", FakeMqttClient),
        pytest.raises(AmpioAuthError),
    ):
        await AmpioClient.test_connection("h", 1883, USER, "bad", info_timeout=0.1)


async def test_connection_raises_connection_error_on_transport_failure() -> None:
    FakeMqttClient.enter_error = aiomqtt.MqttError("Connection refused")
    with (
        patch("ampio_mqtt.client.aiomqtt.Client", FakeMqttClient),
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
    with patch("ampio_mqtt.client.aiomqtt.Client", FakeMqttClient):
        info = await AmpioClient.test_connection("h", 1883, USER, "p", info_timeout=1)
    assert info.mac == 7


# --- request_* and _publish_config when disconnected ----------------------


@pytest.mark.parametrize(
    "method",
    ["request_details", "request_devices", "request_states", "request_info"],
)
async def test_request_methods_raise_when_disconnected(method: str) -> None:
    client = AmpioClient("h", username=USER)
    with pytest.raises(AmpioConnectionError):
        await getattr(client, method)()


# --- stop() and start() lifecycle -----------------------------------------


async def test_stop_cancels_pending_runner() -> None:
    """stop() cancels a runner that is sleeping in the reconnect backoff."""

    async def _sleep_forever(*_: object) -> None:
        await asyncio.sleep(3600)

    client = AmpioClient("h", username=USER, reconnect_interval=3600)
    client._runner = asyncio.create_task(_sleep_forever())
    await client.stop()
    assert client._runner is None


async def test_start_drives_full_discovery_through_mocked_broker() -> None:
    """A scripted broker drives start() through connect + discovery to completion."""
    info_topic = f"ampio/fromDB/{USER}/data/info"
    details_topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    devices_topic = f"ampio/fromDB/{USER}/config/devices"
    states_topic = f"ampio/fromDB/{USER}/data/states"
    FakeMqttClient.scripted_messages = [
        _Message(devices_topic, json.dumps({"List": []}).encode()),
        _Message(details_topic, json.dumps({"List": []}).encode()),
        _Message(states_topic, json.dumps({"List": []}).encode()),
        _Message(info_topic, json.dumps({"Results": {"mac": 99}}).encode()),
    ]
    client = AmpioClient("h", username=USER, reconnect_interval=0.0)
    with patch("ampio_mqtt.client.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=1.0)
    try:
        assert client.available is True
        assert client.server_info is not None and client.server_info.mac == 99
        # The five expected subscriptions were issued.
        assert {
            f"ampio/fromDB/{USER}/config/devicesDetails",
            f"ampio/fromDB/{USER}/config/devices",
            f"ampio/fromDB/{USER}/data/states",
            f"ampio/fromDB/{USER}/data/info",
            f"ampio/fromDB/{USER}/ob/+/state",
        }.issubset(set(FakeMqttClient.subscribed))
    finally:
        await client.stop()


async def test_wait_for_initial_discovery_returns_true_when_all_arrive() -> None:
    """All four discovery messages populate the client and the wait returns True."""
    info_topic = f"ampio/fromDB/{USER}/data/info"
    details_topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    devices_topic = f"ampio/fromDB/{USER}/config/devices"
    states_topic = f"ampio/fromDB/{USER}/data/states"
    FakeMqttClient.scripted_messages = [
        _Message(
            devices_topic,
            json.dumps(
                {"List": [{"id": 17, "mac": 52111, "typ_urzadzenia": 44}]}
            ).encode(),
        ),
        _Message(
            details_topic,
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
        _Message(states_topic, json.dumps({"List": []}).encode()),
        _Message(info_topic, json.dumps({"Results": {"mac": 99}}).encode()),
    ]
    client = AmpioClient("h", username=USER, reconnect_interval=0.0)
    with patch("ampio_mqtt.client.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=1.0)
    try:
        assert await client.wait_for_initial_discovery(timeout=1.0) is True
        assert client.modules and 17 in client.modules
        assert client.objects and 41 in client.objects
        assert client.server_info is not None and client.server_info.mac == 99
    finally:
        await client.stop()


async def test_wait_for_initial_discovery_returns_false_on_timeout() -> None:
    """A partial discovery set leaves the wait returning False without raising."""
    details_topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    devices_topic = f"ampio/fromDB/{USER}/config/devices"
    states_topic = f"ampio/fromDB/{USER}/data/states"
    # No info message scripted -> _info_received never fires.
    FakeMqttClient.scripted_messages = [
        _Message(devices_topic, json.dumps({"List": []}).encode()),
        _Message(details_topic, json.dumps({"List": []}).encode()),
        _Message(states_topic, json.dumps({"List": []}).encode()),
    ]
    client = AmpioClient("h", username=USER, reconnect_interval=0.0)
    with patch("ampio_mqtt.client.aiomqtt.Client", FakeMqttClient):
        await client.start(timeout=2.0, discovery_timeout=0.1)
        try:
            assert await client.wait_for_initial_discovery(timeout=0.1) is False
        finally:
            await client.stop()
