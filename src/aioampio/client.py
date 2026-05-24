"""Async MQTT client for the Ampio DB-object protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import aiomqtt

from .const import (
    DETAILS_REQUEST_PAYLOAD,
    DEVICES_REQUEST_PAYLOAD,
    classify_object,
    config_request_topic,
    details_response_topic,
    devices_response_topic,
    ob_state_wildcard,
)
from .device_types import module_model
from .models import AmpioModule, AmpioObject, AmpioState

_LOGGER = logging.getLogger(__name__)

ObjectListener = Callable[[AmpioObject], None]
AvailabilityListener = Callable[[bool], None]


class AmpioError(Exception):
    """Base error."""


class AmpioConnectionError(AmpioError):
    """Raised when the broker connection fails."""


class AmpioClient:
    """Maintains a connection to the Ampio broker and tracks object state."""

    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        *,
        reconnect_interval: float = 5.0,
    ) -> None:
        """Initialize the client. `username` also namespaces the MQTT topics."""
        self._host = host
        self._port = port
        self._username = username or ""
        self._password = password
        self._reconnect_interval = reconnect_interval

        self.state = AmpioState()
        self._object_listeners: list[ObjectListener] = []
        self._availability_listeners: list[AvailabilityListener] = []

        self._client: aiomqtt.Client | None = None
        self._runner: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._details_received = asyncio.Event()
        self._devices_received = asyncio.Event()
        self._available = False
        self._stop = False

    # --- public API -------------------------------------------------------

    @property
    def objects(self) -> dict[int, AmpioObject]:
        """All known objects keyed by id."""
        return self.state.objects

    @property
    def modules(self) -> dict[int, AmpioModule]:
        """All known physical modules keyed by id."""
        return self.state.modules

    @property
    def sensors(self) -> dict[int, AmpioObject]:
        """Objects classified as sensors."""
        return self.state.sensors

    @property
    def available(self) -> bool:
        """Whether the broker connection is up."""
        return self._available

    def add_object_listener(self, listener: ObjectListener) -> Callable[[], None]:
        """Register a callback invoked on every object update (state/metadata)."""
        self._object_listeners.append(listener)
        return lambda: self._object_listeners.remove(listener)

    def add_availability_listener(
        self, listener: AvailabilityListener
    ) -> Callable[[], None]:
        """Register a callback invoked when connection availability changes."""
        self._availability_listeners.append(listener)
        return lambda: self._availability_listeners.remove(listener)

    @staticmethod
    async def test_connection(
        host: str, port: int, username: str | None, password: str | None
    ) -> None:
        """One-shot connect to validate credentials. Raises on failure."""
        try:
            async with aiomqtt.Client(
                hostname=host,
                port=port,
                username=username,
                password=password,
                identifier=f"aioampio_test_{id(object()):x}",
                timeout=10,
            ):
                return
        except aiomqtt.MqttError as err:
            raise AmpioConnectionError(str(err)) from err

    async def start(
        self, *, timeout: float = 15.0, discovery_timeout: float = 8.0
    ) -> None:
        """Start the connection, wait for connect and initial discovery.

        After connecting, waits up to `discovery_timeout` for the initial
        object and module lists so module names are known before entities are
        created. Restricted accounts may never receive these; the wait then
        simply times out and discovery continues opportunistically.
        """
        self._stop = False
        self._runner = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except TimeoutError as err:
            await self.stop()
            raise AmpioConnectionError("Timed out connecting to Ampio") from err

        waiters = [
            asyncio.create_task(self._details_received.wait()),
            asyncio.create_task(self._devices_received.wait()),
        ]
        _, pending = await asyncio.wait(waiters, timeout=discovery_timeout)
        for task in pending:
            task.cancel()

    async def stop(self) -> None:
        """Stop the connection."""
        self._stop = True
        if self._runner:
            self._runner.cancel()
            with suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None

    async def request_details(self) -> None:
        """Ask the server for the devicesDetails object list."""
        await self._publish_config(DETAILS_REQUEST_PAYLOAD)

    async def request_devices(self) -> None:
        """Ask the server for the physical module list."""
        await self._publish_config(DEVICES_REQUEST_PAYLOAD)

    async def _publish_config(self, keyword: str) -> None:
        if self._client is None:
            raise AmpioConnectionError("Not connected")
        await self._client.publish(
            config_request_topic(self._username), keyword.encode()
        )

    # --- internal ---------------------------------------------------------

    async def _run(self) -> None:
        user = self._username
        while not self._stop:
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                    identifier=f"aioampio_{id(self):x}",
                    timeout=10,
                ) as client:
                    self._client = client
                    await client.subscribe(details_response_topic(user))
                    await client.subscribe(devices_response_topic(user))
                    await client.subscribe(ob_state_wildcard(user))
                    self._set_available(True)
                    self._connected.set()
                    await self.request_devices()
                    await self.request_details()
                    async for message in client.messages:
                        self._handle_message(message)
            except aiomqtt.MqttError as err:
                _LOGGER.debug("Ampio MQTT connection error: %s", err)
            finally:
                self._client = None
                self._set_available(False)
            if not self._stop:
                await asyncio.sleep(self._reconnect_interval)

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        for listener in list(self._availability_listeners):
            listener(available)

    def _handle_message(self, message: aiomqtt.Message) -> None:
        topic = str(message.topic)
        payload = message.payload
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")

        if topic == details_response_topic(self._username):
            self._handle_details(payload)
        elif topic == devices_response_topic(self._username):
            self._handle_devices(payload)
        elif topic.endswith("/state") and "/ob/" in topic:
            self._handle_state(topic, payload)

    def _handle_details(self, payload: Any) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Could not parse Ampio devicesDetails")
            return
        for item in data.get("List", []):
            oid = _to_int(item.get("id"))
            if oid is None:
                continue
            typ = item.get("typ_komponentu")
            interp = _to_int(item.get("interpretacja"))
            obj = self.state.objects.get(oid) or AmpioObject(id=oid)
            obj.device_id = _to_int(item.get("id_urzadzenia"))
            obj.typ_komponentu = typ
            obj.name = item.get("opis_menu") or obj.name
            obj.interpretacja = interp
            obj.funkcja = _to_int(item.get("funkcja"))
            obj.room_id = _to_int(item.get("lokalizacja"))
            obj.min = _to_float(item.get("min"))
            obj.max = _to_float(item.get("max"))
            obj.kind = classify_object(typ, interp)
            if obj.value is None:
                self._seed_value(obj, item.get("stan_json"))
            self.state.objects[oid] = obj
            self._notify(obj)
        self._details_received.set()

    @staticmethod
    def _seed_value(obj: AmpioObject, stan_json: Any) -> None:
        """Seed the last-known value from the metadata `stan_json` field."""
        if not stan_json:
            return
        try:
            data = json.loads(stan_json)
        except (ValueError, TypeError):
            return
        obj.value = data.get("state", obj.value)
        obj.desc = data.get("desc", obj.desc)

    def _handle_devices(self, payload: Any) -> None:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            _LOGGER.warning("Could not parse Ampio devices list")
            return
        for item in data.get("List", []):
            mid = _to_int(item.get("id"))
            if mid is None:
                continue
            typ = _to_int(item.get("typ_urzadzenia"))
            self.state.modules[mid] = AmpioModule(
                id=mid,
                mac=_to_int(item.get("mac")),
                name=item.get("nazwa_urzadzenia") or None,
                type=typ,
                model=module_model(typ),
                sw_version=_to_int(item.get("wersja_softu")),
                hw_version=_to_int(item.get("wersja_pcb")),
            )
        self._devices_received.set()

    def _handle_state(self, topic: str, payload: str) -> None:
        parts = topic.split("/")
        # ampio / fromDB / <user> / ob / <id> / state
        if len(parts) < 6 or parts[3] != "ob":
            return
        oid = _to_int(parts[4])
        if oid is None:
            return
        value = payload
        desc = None
        try:
            data = json.loads(payload)
            value = data.get("state", payload)
            desc = data.get("desc")
        except (ValueError, TypeError):
            pass

        obj = self.state.objects.get(oid)
        if obj is None:
            # No metadata yet (e.g. restricted account) -> generic sensor.
            obj = AmpioObject(id=oid, kind=classify_object(None, None))
            self.state.objects[oid] = obj
        obj.value = value
        obj.desc = desc
        self._notify(obj)

    def _notify(self, obj: AmpioObject) -> None:
        for listener in list(self._object_listeners):
            listener(obj)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
