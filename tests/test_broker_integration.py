"""Public client behavior over real MQTT sockets with synthetic M-SERV replies."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import aiomqtt
import pytest
from conftest import details, devices, info, params_table, snapshot

from ampio_mqtt import (
    AmpioAuthError,
    AmpioClient,
    AuthFailed,
    AvailabilityChanged,
    ObjectUpdated,
)

pytestmark = pytest.mark.mqtt
_HOST = "127.0.0.1"
_PASSWORD = "synthetic-test-password"


class Broker:
    def __init__(self, directory: Path, executable: str, password_tool: str) -> None:
        self.directory = directory
        self.executable = executable
        self.password_tool = password_tool
        self.process: asyncio.subprocess.Process | None = None
        # Held open until the broker is spawned. A bind-then-close reservation
        # leaves the port free for any other process between the two, and the
        # loser of that race is a failed required check.
        self._reservation: socket.socket | None = socket.socket()
        self._reservation.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._reservation.bind((_HOST, 0))
        self.port = self._reservation.getsockname()[1]
        self.password_file = directory / "passwords"
        self.configuration = directory / "mosquitto.conf"
        self.configuration.write_text(
            f"listener {self.port} {_HOST}\n"
            "allow_anonymous false\n"
            f"password_file {self.password_file}\n"
            "persistence false\n"
        )

    async def set_password(self, username: str, password: str) -> None:
        flags = ["-b"]
        if not self.password_file.exists():
            flags.append("-c")
        process = await asyncio.create_subprocess_exec(
            self.password_tool, *flags, str(self.password_file), username, password
        )
        assert await process.wait() == 0

    async def release_port(self) -> None:
        if self._reservation is not None:
            self._reservation.close()
            self._reservation = None

    async def start(self) -> None:
        await self.release_port()
        log_path = self.directory / "broker.log"
        with log_path.open("ab") as log:
            self.process = await asyncio.create_subprocess_exec(
                self.executable,
                "-c",
                str(self.configuration),
                stdout=log,
                stderr=log,
            )
        async with asyncio.timeout(5):
            while True:
                if self.process.returncode is not None:
                    pytest.fail(log_path.read_text())
                try:
                    _, writer = await asyncio.open_connection(_HOST, self.port)
                except OSError:
                    await asyncio.sleep(0.01)
                else:
                    writer.close()
                    await writer.wait_closed()
                    return

    async def stop(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                async with asyncio.timeout(5):
                    await self.process.wait()
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

    def session(self, username: str = "u") -> aiomqtt.Client:
        return aiomqtt.Client(
            _HOST, port=self.port, username=username, password=_PASSWORD, timeout=3
        )

    def client(self, username: str = "u", password: str = _PASSWORD) -> AmpioClient:
        return AmpioClient(
            _HOST,
            username,
            password,
            port=self.port,
            reconnect_interval=0.1,
        )


@pytest.fixture
async def broker(
    request: pytest.FixtureRequest, tmp_path: Path
) -> AsyncIterator[Broker]:
    if not request.config.getoption("--mqtt"):
        pytest.skip("Pass --mqtt to run local broker integration tests")
    executable = shutil.which("mosquitto")
    password_tool = shutil.which("mosquitto_passwd")
    if executable is None or password_tool is None:
        pytest.fail("--mqtt requires mosquitto and mosquitto_passwd on PATH")
    instance = Broker(tmp_path, executable, password_tool)
    try:
        await instance.set_password("u", _PASSWORD)
        await instance.set_password("admin", _PASSWORD)
        await instance.start()
        yield instance
    finally:
        await instance.stop()
        await instance.release_port()


@contextlib.asynccontextmanager
async def responding(
    broker: Broker, username: str = "u", *, state: str | None = "0"
) -> AsyncIterator[aiomqtt.Client]:
    row = {
        "id": 5,
        "id_urzadzenia": 2,
        "typ_komponentu": "flaga",
        "leafId": "0_a_76_0_0",
        "funkcja": 1,
    }
    table = {
        ("info", ""): (
            "data/info",
            info(userId=-1 if username == "admin" else 7, serverVersion="1865"),
        ),
        ("states", ""): (
            "data/states",
            snapshot()
            if state is None
            else snapshot(
                {
                    "id": 5,
                    "stan_json": json.dumps(
                        {"state": state, "on": 1000 if state == "0" else 2000}
                    ),
                }
            ),
        ),
        ("config", "devices"): ("config/devices", devices({"id": 2, "mac": 10})),
        ("data", "devices"): ("data/devices", details(row)),
        ("data", "params_devices"): ("data/params_devices", params_table({"id": 5})),
    }
    async with broker.session(username) as session:
        await session.subscribe(f"ampio/control/{username}/+", qos=1)

        async def respond() -> None:
            async for message in session.messages:
                key = (str(message.topic).rsplit("/", 1)[1], message.payload.decode())
                if key in table:
                    suffix, payload = table[key]
                    await session.publish(
                        f"ampio/fromDB/{username}/{suffix}", payload, qos=1
                    )

        task = asyncio.create_task(respond())
        try:
            yield session
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("username", ["u", "admin"])
async def test_discovery_and_live_updates(broker: Broker, username: str) -> None:
    client = broker.client(username)
    changed = asyncio.Event()

    def on_object(event: ObjectUpdated) -> None:
        if event.object.state == "1":
            changed.set()

    client.subscribe(on_object, of=ObjectUpdated)
    try:
        async with responding(broker, username) as session:
            assert await client.connect(timeout=3, discovery_timeout=3)
            assert client.objects[5].state == "0"
            assert client.server_info is not None
            assert client.server_info.mac == 1
            assert (
                client.diagnostics_snapshot()["connection"]["subscribe_failures"] == {}
            )
            if username == "admin":
                assert client.modules[2].mac == 10
            await session.publish(
                f"ampio/fromDB/{username}/ob/5/state",
                json.dumps({"state": "1", "on": 1789000000000}),
                qos=1,
            )
            await asyncio.wait_for(changed.wait(), 3)
            assert client.objects[5].is_on
    finally:
        await client.disconnect()


async def test_reconnect_after_broker_restart(broker: Broker) -> None:
    client = broker.client()
    offline, online, changed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def on_availability(event: AvailabilityChanged) -> None:
        (online if event.available else offline).set()

    def on_object(event: ObjectUpdated) -> None:
        if event.object.state == "1":
            changed.set()

    client.subscribe(on_availability, of=AvailabilityChanged)
    client.subscribe(on_object, of=ObjectUpdated)
    try:
        async with responding(broker):
            assert await client.connect(timeout=3, discovery_timeout=3)
        await broker.stop()
        await asyncio.wait_for(offline.wait(), 3)
        assert not client.available
        online.clear()
        await broker.start()
        async with responding(broker) as session:
            await asyncio.wait_for(online.wait(), 3)
            await session.publish(
                "ampio/fromDB/u/ob/5/state",
                json.dumps({"state": "1", "on": 1789000000000}),
                qos=1,
            )
            await asyncio.wait_for(changed.wait(), 3)
            assert client.available
            assert client.diagnostics_snapshot()["connection"]["reconnect_count"] >= 1
    finally:
        await client.disconnect()


async def test_initial_auth_rejection(broker: Broker) -> None:
    client = broker.client(password="incorrect-test-password")
    try:
        with pytest.raises(AmpioAuthError):
            await client.connect(timeout=3, discovery_timeout=3)
        assert not client.available
    finally:
        await client.disconnect()


async def test_auth_rejection_after_broker_restart(broker: Broker) -> None:
    client = broker.client()
    rejected = asyncio.Event()
    transitions: list[bool | str] = []
    client.subscribe(
        lambda event: transitions.append(event.available), of=AvailabilityChanged
    )

    def on_auth(event: AuthFailed) -> None:
        transitions.append("auth")
        rejected.set()

    client.subscribe(on_auth, of=AuthFailed)
    try:
        async with responding(broker):
            assert await client.connect(timeout=3, discovery_timeout=3)
        await broker.stop()
        await broker.set_password("u", "replacement-test-password")
        await broker.start()
        await asyncio.wait_for(rejected.wait(), 3)
        # The order is the point, not the count: the session came up, went
        # down with the broker, and ended on the rejection. A reconnect that
        # flaps twice on the way is not a failure of this behavior.
        assert transitions[0] is True
        assert transitions[-1] == "auth"
        assert transitions.count("auth") == 1
        assert False in transitions
        assert not client.available
    finally:
        await client.disconnect()


async def test_retained_raw_state_is_replayed_without_live_health(
    broker: Broker,
) -> None:
    client = broker.client("admin")
    try:
        async with responding(broker, "admin", state=None) as session:
            await session.publish("ampio/from/A/state/f/1", "1", qos=1, retain=True)
            assert await client.connect(timeout=3, discovery_timeout=3)
            assert client.objects[5].is_on
            assert client.modules[2].last_seen is None
    finally:
        await client.disconnect()
