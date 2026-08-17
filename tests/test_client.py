"""Tests for the AmpioClient surface: listener wiring, lifecycle entry
points, diagnostics retention, stats, and the model properties consumers
read. The store's message semantics are covered in test_store.py."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import aiomqtt
import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from ampio_mqtt import AccessTier, AmpioAuthError, AmpioClient, AmpioObject

USER = "u"


def _flaga(oid: int, funkcja: int, dev: int = 7) -> dict:
    return {
        "id": oid,
        "id_urzadzenia": dev,
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "funkcja": funkcja,
        "opis_menu": "Flag",
    }


def _details(*items) -> bytes:
    return json.dumps({"Status": 0, "List": list(items)}).encode()


def _devices(*items) -> bytes:
    return json.dumps({"List": list(items)}).encode()


def _info(**fields_) -> bytes:
    return json.dumps({"Results": fields_}).encode()


def _client() -> AmpioClient:
    return AmpioClient("host", username=USER)


def test_mserv_id_prefers_info_mac_cross_check() -> None:
    """mserv_id matches the module whose mac_global matches info.mac."""
    client = _client()
    # Two modules with typ != 10 plus the actual M-SERV (typ=10, mac_global=47846).
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices(
            {
                "id": 1,
                "mac": 1,
                "mac_global": 47846,
                "typ_urzadzenia": 10,
                "nazwa_urzadzenia": "MSERV",
            },
            {
                "id": 2,
                "mac": 2,
                "mac_global": 1000,
                "typ_urzadzenia": 4,
                "nazwa_urzadzenia": "MREL",
            },
        ),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/data/info",
        _info(serverVersion="1", mac="47846"),
    )
    assert client.mserv_id == 1


def test_mserv_id_falls_back_to_typ10_without_info() -> None:
    """Without info, a unique typ_urzadzenia=10 module identifies the M-SERV."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices(
            {
                "id": 5,
                "mac": 1,
                "mac_global": 12345,
                "typ_urzadzenia": 10,
                "nazwa_urzadzenia": "MSERV",
            },
            {
                "id": 6,
                "mac": 2,
                "mac_global": 67890,
                "typ_urzadzenia": 4,
                "nazwa_urzadzenia": "MREL",
            },
        ),
    )
    assert client.mserv_id == 5


def test_mserv_id_none_when_ambiguous_and_no_info() -> None:
    """If multiple modules are typ=10 and no info reply, do not guess."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices(
            {
                "id": 1,
                "mac": 1,
                "mac_global": 1,
                "typ_urzadzenia": 10,
                "nazwa_urzadzenia": "MSERV-A",
            },
            {
                "id": 2,
                "mac": 2,
                "mac_global": 2,
                "typ_urzadzenia": 10,
                "nazwa_urzadzenia": "MSERV-B",
            },
        ),
    )
    assert client.mserv_id is None


def test_state_updates_object_and_notifies() -> None:
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {
                "id": 41,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "Salon",
            }
        ),
    )
    received: list = []
    client.add_object_listener(received.append)

    client._feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{ "state": "22.5","desc": "22.5 C" , "on": 1779555459594} ',
    )
    obj = client.objects[41]
    assert obj.value == "22.5"
    assert received == [obj]


def test_object_removal_listener_fires_after_eviction() -> None:
    client = _client()
    removed: list[int] = []
    unsubscribe = client.add_object_removal_listener(lambda o: removed.append(o.id))
    topic = f"ampio/fromDB/{USER}/config/devicesDetails"
    client._feed_message(topic, _details(_flaga(41, 3), _flaga(42, 4)))
    client._feed_message(topic, _details(_flaga(41, 3)))
    assert removed == [42]
    assert 42 not in client.objects

    unsubscribe()
    client._feed_message(topic, _details(_flaga(41, 3), _flaga(42, 4)))
    client._feed_message(topic, _details(_flaga(41, 3)))
    assert removed == [42]


def test_availability_listener() -> None:
    client = _client()
    events: list[bool] = []
    client.add_availability_listener(events.append)
    client._connection._set_available(True)
    client._connection._set_available(True)
    client._connection._set_available(False)
    assert events == [True, False]


class _AuthFailingClient:
    """aiomqtt.Client stand-in whose context manager raises an auth error."""

    def __init__(self, *args, **kwargs) -> None:
        self.messages = self  # any iterable - won't be reached

    async def __aenter__(self):
        raise aiomqtt.MqttCodeError(ReasonCode(PacketTypes.CONNACK, "Not authorized"))

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def test_start_raises_auth_error_on_credential_rejection() -> None:
    """A broker auth rejection during _run surfaces AmpioAuthError from start()."""
    client = AmpioClient("host", username="u", password="bad", reconnect_interval=0.0)
    with (
        patch("ampio_mqtt._connection.aiomqtt.Client", _AuthFailingClient),
        pytest.raises(AmpioAuthError),
    ):
        await client.start(timeout=2.0, discovery_timeout=0.1)
    assert client._connection._runner is None  # stop() ran during the raise


async def test_start_times_out_without_auth_error() -> None:
    """A connection that simply never comes up still raises AmpioConnectionError."""

    class _Stuck:
        def __init__(self, *a, **k):
            self.messages = self

        async def __aenter__(self):
            await asyncio.sleep(10)

        async def __aexit__(self, *a):
            return False

    client = AmpioClient("host", username="u", password="p", reconnect_interval=0.0)
    from ampio_mqtt import AmpioConnectionError

    with (
        patch("ampio_mqtt._connection.aiomqtt.Client", _Stuck),
        pytest.raises(AmpioConnectionError),
    ):
        await client.start(timeout=0.5, discovery_timeout=0.1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("0", False), ("1", True), ("255", True)],
)
def test_is_on_interpretation(value, expected) -> None:
    assert AmpioObject(id=1, value=value).is_on is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("23.5", 23.5),
        ("255", 255.0),
        ("0", 0.0),
        ("-4.2", -4.2),
        (None, None),
        ("", None),
        ("open", None),
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("1e999", None),
    ],
)
def test_numeric_value_interpretation(value, expected) -> None:
    assert AmpioObject(id=1, value=value).numeric_value == expected


def test_last_payloads_retained_for_each_handler() -> None:
    """Each discovery handler stashes the verbatim payload for diagnostics."""
    client = _client()
    devices = _devices({"id": 1, "mac": 1, "typ_urzadzenia": 10})
    details = _details(
        {"id": 5, "id_urzadzenia": 1, "typ_komponentu": "temp", "interpretacja": 1}
    )
    info = _info(mac=12345, serverVersion="2025")

    client._feed_message(f"ampio/fromDB/{USER}/config/devices", devices)
    client._feed_message(f"ampio/fromDB/{USER}/config/devicesDetails", details)
    client._feed_message(f"ampio/fromDB/{USER}/data/info", info)

    assert client.last_payloads["devices"] == devices.decode()
    assert client.last_payloads["details"] == details.decode()
    assert client.last_payloads["info"] == info.decode()


def test_groups_payloads_are_retained() -> None:
    """`data/groups` and `data/group_devices` populate the last_payloads map."""
    client = _client()
    groups = json.dumps({"List": [{"id": 1, "opis_menu": "Salon"}]}).encode()
    group_devices = json.dumps({"List": [{"id_grupy": 1, "id_obiektu": 5}]}).encode()
    client._feed_message(f"ampio/fromDB/{USER}/data/groups", groups)
    client._feed_message(f"ampio/fromDB/{USER}/data/group_devices", group_devices)
    assert client.last_payloads["groups"] == groups.decode()
    assert client.last_payloads["group_devices"] == group_devices.decode()


def test_access_tier_reads_the_account_id_off_the_info_reply() -> None:
    client = _client()
    assert client.access_tier is AccessTier.UNKNOWN
    client._feed_message(
        f"ampio/fromDB/{USER}/data/info", b'{"Results": {"mac": 1, "userId": "4"}}'
    )
    assert client.access_tier is AccessTier.RESTRICTED
    client._feed_message(
        f"ampio/fromDB/{USER}/data/info", b'{"Results": {"mac": 1, "userId": "-1"}}'
    )
    assert client.access_tier is AccessTier.ADMIN


@pytest.mark.parametrize(
    ("leaf_id", "expected"),
    [("0_cb9b_74_0_1", "leaf_0_cb9b_74_0_1"), ("", None)],
)
def test_stable_key_from_leaf_id(leaf_id: str, expected: str | None) -> None:
    assert AmpioObject(id=1, leaf_id=leaf_id).stable_key == expected


def test_dispatch_updates_last_message_at() -> None:
    """Every dispatched MQTT message advances the connection stats clock."""
    client = _client()
    assert client.stats.last_message_at is None
    client._feed_message(f"ampio/fromDB/{USER}/data/info", _info(mac=1))
    assert client.stats.last_message_at is not None
    first = client.stats.last_message_at
    client._feed_message(f"ampio/fromDB/{USER}/data/info", _info(mac=1))
    assert client.stats.last_message_at >= first


async def test_reconnect_count_increments_on_reconnect() -> None:
    """A poison-then-recover cycle bumps reconnect_count and captures the error."""

    attempts = 0
    keep_running = asyncio.Event()

    class _Flapping:
        def __init__(self, *args, **kwargs) -> None:
            self.messages = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def subscribe(self, topic, qos=0):  # pragma: no cover - trivial
            return [0 for _ in topic] if isinstance(topic, list) else [0]

        async def publish(self, _topic, _payload=b"", qos=0):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # First connection: raise mid-iteration so the runner reconnects.
                raise aiomqtt.MqttError("simulated drop")
            # Second connection: keep the runner alive long enough for the test
            # to observe the reconnect_count increment, then end.
            await keep_running.wait()
            raise StopAsyncIteration

    client = AmpioClient("host", username=USER, password="p", reconnect_interval=0.0)
    runner_task: asyncio.Task[None]

    async def _run() -> None:
        with patch("ampio_mqtt._connection.aiomqtt.Client", _Flapping):
            client._connection._stop = False
            await client._connection._run()

    runner_task = asyncio.create_task(_run())
    # Wait for the second connection to land (reconnect_count incremented).
    for _ in range(100):
        if client.stats.reconnect_count >= 1:
            break
        await asyncio.sleep(0.01)
    keep_running.set()
    client._connection._stop = True
    await runner_task

    assert client.stats.reconnect_count == 1
    assert client.stats.started_at is not None
    assert client.stats.last_error == "simulated drop"


@pytest.mark.parametrize(
    ("typ", "leaf_id", "is_system", "visible"),
    [
        # Real object with a non-empty leafId (the real-install shape).
        ("temp", "0_cb8f_76_0_0", False, True),
        # Ghost: empty leafId, not a system type.
        ("temp", "", False, False),
        # Named-output ghost on the M-SERV - the canonical Matter-leak case.
        ("przekaznik", "", False, False),
        # System objects are visible regardless of leafId.
        ("symulacja", "", True, True),
        ("detekcja", "", True, True),
        # `flaga` is an input but NOT a system object, so it needs its leafId.
        ("flaga", "", False, False),
        ("flaga", "0_d09a_3_0_1", False, True),
        # Unclassified / missing typ_komponentu - treat as non-system.
        (None, "", False, False),
        (None, "0_x_x_x_x", False, True),
    ],
)
def test_visibility_predicate(
    typ: str | None,
    leaf_id: str,
    is_system: bool,
    visible: bool,
) -> None:
    obj = AmpioObject(id=1, typ_komponentu=typ, leaf_id=leaf_id)
    assert obj.is_system is is_system
    assert obj.visible is visible


@pytest.mark.parametrize(
    ("params", "hidden"),
    [
        (0, False),  # absent -> no flags
        (1, False),  # bit 0 only (every real object carries it)
        (16, True),  # bit 4 -> hidden stub
        (17, True),  # bit 0 + bit 4 (the live phantom shape)
        (1 << 37, False),  # a Matter opt-in is not a visibility signal
        ((1 << 37) | 16, True),  # opted in AND hidden -> hidden still wins
    ],
)
def test_params_flags(params: int, hidden: bool) -> None:
    obj = AmpioObject(id=1, params=params)
    assert obj.hidden is hidden


def test_hidden_overrides_leaf_id_visibility() -> None:
    """Bit 4 (hidden) drops an object even when its leaf_id would show it.

    This is the duplicated-Designer-channel case: a phantom and its labelled
    twin share a leaf_id, so the leaf_id heuristic keeps both and the consumer's
    unique-id collides. The phantom carries bit 4, so it is filtered out.
    """
    phantom = AmpioObject(
        id=1, typ_komponentu="lin_wej", leaf_id="0_cb97_74_0_1", params=17
    )
    labelled = AmpioObject(
        id=2, typ_komponentu="lin_wej", leaf_id="0_cb97_74_0_1", params=(1 << 37) | 1
    )
    assert phantom.visible is False
    assert labelled.visible is True
    # A system object the M-SERV explicitly hid (bit 4) is dropped too, even
    # though is_system would otherwise force it visible.
    assert AmpioObject(id=3, typ_komponentu="symulacja", params=16).visible is False
