"""Tests for AmpioClient DB-object message handling (no real broker)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import fields
from unittest.mock import patch

import aiomqtt
import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from ampio_mqtt import AccessTier, AmpioAuthError, AmpioClient, AmpioObject

USER = "u"

# A module whose mac is 0xCAFE, so its raw topics are `ampio/from/CAFE/...`.
_PANEL = {"id": 7, "mac": 0xCAFE, "typ_urzadzenia": 11, "nazwa_urzadzenia": "panel"}


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


def _states(*items) -> bytes:
    return json.dumps({"List": list(items)}).encode()


def _info(**fields_) -> bytes:
    return json.dumps({"Results": fields_}).encode()


def _client() -> AmpioClient:
    return AmpioClient("host", username=USER)


def test_details_populate_and_classify() -> None:
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {
                "id": 41,
                "id_urzadzenia": 3,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "Salon",
            },
            {
                "id": 107,
                "id_urzadzenia": 3,
                "typ_komponentu": "lin_wej",
                "interpretacja": 7,
                "opis_menu": "CO2",
            },
            {
                "id": 1,
                "id_urzadzenia": 1,
                "typ_komponentu": "przekaznik",
                "interpretacja": 1,
                "opis_menu": "Pump",
            },
        ),
    )

    assert set(client.objects) == {41, 107, 1}
    temp = client.objects[41]
    assert temp.kind is not None and temp.kind.device_class == "temperature"
    assert temp.name == "Salon" and temp.device_id == 3
    # The raw `interpretacja` selector is retained on the object for consumers,
    # alongside the resolved `kind` the library derives from it.
    assert client.objects[107].interpretacja == 7
    # relay is not a sensor
    assert client.objects[1].is_sensor is False
    assert set(client.sensors) == {41, 107}


def test_devices_populate_modules_with_model_and_versions() -> None:
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices(
            {
                "id": 17,
                "mac": 52111,
                "typ_urzadzenia": 44,
                "nazwa_urzadzenia": "m-sens salon",
                "wersja_softu": 63,
                "wersja_pcb": 7,
            },
            {
                "id": 99,
                "mac": 1,
                "typ_urzadzenia": 999,
                "nazwa_urzadzenia": "Mystery",
                "wersja_softu": 1,
                "wersja_pcb": 2,
            },
        ),
    )

    mod = client.modules[17]
    assert mod.name == "m-sens salon"
    assert mod.type == 44
    assert mod.model == "M-SENS"
    assert mod.sw_version == 63
    assert mod.hw_version == 7
    # Unknown type code -> no model name, but the module is still tracked.
    assert client.modules[99].model is None


def test_state_updates_module_last_seen_with_local_receive_time() -> None:
    """A live push marks the module seen at local receive time - the server's
    `on` date is state provenance, never liveness evidence (one clock only)."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {
                "id": 41,
                "id_urzadzenia": 17,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
            }
        ),
    )
    assert client.modules[17].last_seen is None

    before = time.time()
    client._feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state": "22.5", "on": 1779565263813}',
    )
    first_seen = client.modules[17].last_seen
    assert first_seen is not None and before <= first_seen <= time.time()

    # Another push refreshes it, regardless of its server date being older.
    client._feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state": "21.0", "on": 1779560000000}',
    )
    later_seen = client.modules[17].last_seen
    assert later_seen is not None and later_seen >= first_seen


def test_state_push_with_numeric_state_is_stored_as_string() -> None:
    """A broker that emits unquoted numbers in `state` still yields str value."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {
                "id": 41,
                "id_urzadzenia": 17,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
            }
        ),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state": 24.4, "on": 1779560000000}',
    )
    value = client.objects[41].value
    assert value == "24.4"
    assert isinstance(value, str)


def test_states_snapshot_seeds_value_without_touching_last_seen() -> None:
    """The bulk states reply seeds the value but is not liveness evidence:
    it replays DB state that may be arbitrarily old, so last_seen stays
    None until a live message arrives."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {
                "id": 41,
                "id_urzadzenia": 17,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
            }
        ),
    )
    assert client.objects[41].value is None
    assert client.modules[17].last_seen is None

    client._feed_message(
        f"ampio/fromDB/{USER}/data/states",
        _states(
            {
                "id": 41,
                "stan_json": '{"state": "22.5", "on": 1779560000000}',
                "upTime": 600,
            }
        ),
    )
    assert client.objects[41].value == "22.5"
    assert client.objects[41].updated_at == 1779560000.0
    assert client.modules[17].last_seen is None


def test_states_snapshot_does_not_overwrite_live_value() -> None:
    """A snapshot does not regress a value already set by a live push."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {"id": 41, "typ_komponentu": "temp", "interpretacja": 1, "opis_menu": "T"}
        ),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state": "fresh", "on": 1779570000000}',
    )
    assert client.objects[41].value == "fresh"

    client._feed_message(
        f"ampio/fromDB/{USER}/data/states",
        _states({"id": 41, "stan_json": '{"state": "stale", "on": 1779560000000}'}),
    )
    assert client.objects[41].value == "fresh"


def test_states_snapshot_creates_placeholder_for_unknown_object() -> None:
    """A state for an object whose metadata is not yet known is still tracked."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/data/states",
        _states({"id": 999, "stan_json": '{"state": "1", "on": 1779560000000}'}),
    )
    assert client.objects[999].value == "1"
    # The kind is the generic fallback because no metadata existed.
    assert client.objects[999].kind is not None
    assert client.objects[999].kind.key == "value"


def test_details_stan_json_seed_does_not_touch_last_seen() -> None:
    """The catalogue's stan_json seed carries state, not liveness - like the
    bulk snapshot, it replays DB rows and leaves last_seen alone."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {
                "id": 41,
                "id_urzadzenia": 17,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
                "stan_json": '{"state": "22.5", "on": 1779560000000}',
            }
        ),
    )
    assert client.objects[41].value == "22.5"
    assert client.modules[17].last_seen is None


def test_info_parses_only_safe_fields() -> None:
    """Server info parsing keeps version/ip/mac but drops geo/cloud/private fields."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/data/info",
        _info(
            serverVersion="1865",
            serverRevision="409",
            mqttVersion="5.133.11",
            local_ip="10.0.0.1",
            device_id="0011223344556677",
            mac="47846",
            # Private fields that must not be stored on AmpioServerInfo.
            lat="51.0",
            lon="17.0",
            city="Some Street",
            cloudInfo="abc.example.com",
            publicKey="xxx",
            perm="0",
        ),
    )
    info = client.server_info
    assert info is not None
    assert info.mac == 47846
    assert info.server_version == "1865"
    assert info.server_revision == "409"
    assert info.mqtt_version == "5.133.11"
    assert info.local_ip == "10.0.0.1"
    assert info.device_id == "0011223344556677"
    stored = {f.name for f in fields(info)}
    for forbidden in ("lat", "lon", "city", "cloudInfo", "publicKey", "perm"):
        assert forbidden not in stored


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


def test_devices_redelivery_preserves_last_seen() -> None:
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client._store.state.modules[17].last_seen = 1700000000.0
    # Re-deliver the devices list (e.g. on reconnect) - last_seen must persist.
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m2"}),
    )
    assert client.modules[17].name == "m2"
    assert client.modules[17].last_seen == 1700000000.0


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


def test_state_without_metadata_creates_generic_sensor() -> None:
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/ob/93/state",
        b'{"state":"187.6","desc":"187.6 "}',
    )
    obj = client.objects[93]
    assert obj.is_sensor is True  # generic fallback
    assert obj.value == "187.6"


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


@pytest.mark.parametrize(
    "topic_suffix",
    [
        "config/devicesDetails",
        "config/devices",
        "data/states",
        "data/devices",
        "data/params_devices",
    ],
)
def test_handlers_log_and_skip_unparseable_payloads(
    caplog: pytest.LogCaptureFixture, topic_suffix: str
) -> None:
    client = _client()
    with caplog.at_level("WARNING", logger="ampio_mqtt.client"):
        client._feed_message(f"ampio/fromDB/{USER}/{topic_suffix}", b"not json")
    assert "Could not parse" in caplog.text


def test_dispatch_ignores_unmatched_topics() -> None:
    """A topic that matches none of the four patterns is silently ignored."""
    client = _client()
    client._feed_message("totally/unrelated/topic", b"anything")
    assert client.objects == {}
    assert client.modules == {}


def test_state_with_unparseable_payload_is_dropped() -> None:
    """An `/ob/<non-int>/state` topic is rejected without raising."""
    client = _client()
    client._feed_message(f"ampio/fromDB/{USER}/ob/not-an-int/state", b"x")
    assert client.objects == {}


def test_stan_json_with_no_state_field_does_not_overwrite_value() -> None:
    """A stan_json blob without `state` should not clobber an existing value."""
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {
                "id": 41,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "T",
                "stan_json": '{"on": 1779560000000}',  # no "state"
            }
        ),
    )
    assert client.objects[41].value is None


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


# --- raw-channel input bridge ---------------------------------------------


def _client_with_panel_flag() -> AmpioClient:
    """Client that knows panel module 7 (mac CAFE) and a flaga at funkcja 32."""
    client = _client()
    client._feed_message(f"ampio/fromDB/{USER}/config/devices", _devices(_PANEL))
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails", _details(_flaga(50, 32))
    )
    return client


def test_details_classify_input_and_funkcja() -> None:
    client = _client_with_panel_flag()
    obj = client.objects[50]
    assert obj.is_input is True
    assert obj.kind is not None and obj.kind.key == "flaga"
    assert obj.funkcja == 32
    assert obj.is_sensor is False


def test_raw_channel_routes_to_input_object_and_notifies() -> None:
    client = _client_with_panel_flag()
    received: list = []
    client.add_object_listener(received.append)

    client._feed_message("ampio/from/CAFE/state/f/32", b"1")

    obj = client.objects[50]
    assert obj.value == "1" and obj.is_on is True
    assert received == [obj]


def test_raw_channel_unmapped_is_ignored() -> None:
    client = _client_with_panel_flag()
    received: list = []
    client.add_object_listener(received.append)

    # funkcja 5 has no object; a different module mac has no objects at all.
    client._feed_message("ampio/from/CAFE/state/f/5", b"1")
    client._feed_message("ampio/from/BEEF/state/f/32", b"1")

    assert client.objects[50].value is None
    assert received == []


def test_raw_channel_malformed_topic_is_ignored() -> None:
    """A topic that passes the dispatch filter but fails the parser is dropped."""
    client = _client_with_panel_flag()
    client._feed_message("ampio/from/CAFE/state/f", b"1")  # too short
    assert client.objects[50].value is None


def test_index_rebuilds_when_devices_arrive_after_details() -> None:
    client = _client()
    # Details first: module mac unknown, so the flag is not yet routable.
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails", _details(_flaga(50, 32))
    )
    client._feed_message("ampio/from/CAFE/state/f/32", b"1")
    assert client.objects[50].value is None  # not routed - no module mac yet

    # Devices arrive -> index rebuilds -> now routable.
    client._feed_message(f"ampio/fromDB/{USER}/config/devices", _devices(_PANEL))
    client._feed_message("ampio/from/CAFE/state/f/32", b"1")
    assert client.objects[50].value == "1"


def test_flag_without_funkcja_is_not_indexed() -> None:
    client = _client()
    client._feed_message(f"ampio/fromDB/{USER}/config/devices", _devices(_PANEL))
    no_funkcja = {
        "id": 51,
        "id_urzadzenia": 7,
        "typ_komponentu": "flaga",
        "interpretacja": 1,
        "opis_menu": "Flag",
    }
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails", _details(no_funkcja)
    )
    assert client._store._input_index == {}


def test_per_object_echo_dropped_after_raw_seen() -> None:
    """Once raw is seen, the slower per-object echo is suppressed."""
    client = _client_with_panel_flag()
    received: list = []
    client.add_object_listener(received.append)

    client._feed_message("ampio/from/CAFE/state/f/32", b"1")
    assert received == [client.objects[50]]

    received.clear()
    # The lagging per-object republish (note the different "255" encoding).
    client._feed_message(
        f"ampio/fromDB/{USER}/ob/50/state", b'{"state": "255", "on": 1700}'
    )
    assert received == []  # no double notify
    assert client.objects[50].value == "1"  # fast raw value preserved


def test_mapped_input_without_raw_uses_per_object_fallback() -> None:
    """A mapped input that never produced a raw edge still updates per-object."""
    client = _client_with_panel_flag()
    received: list = []
    client.add_object_listener(received.append)

    client._feed_message(
        f"ampio/fromDB/{USER}/ob/50/state", b'{"state": "255", "on": 1700}'
    )
    obj = client.objects[50]
    assert obj.value == "255" and obj.is_on is True
    assert received == [obj]


def test_detekcja_routes_via_digital_input_prefix() -> None:
    client = _client()
    client._feed_message(f"ampio/fromDB/{USER}/config/devices", _devices(_PANEL))
    det = {
        "id": 60,
        "id_urzadzenia": 7,
        "typ_komponentu": "detekcja",
        "interpretacja": 1,
        "funkcja": 4,
        "opis_menu": "Motion",
    }
    client._feed_message(f"ampio/fromDB/{USER}/config/devicesDetails", _details(det))
    client._feed_message("ampio/from/CAFE/state/i/4", b"1")
    obj = client.objects[60]
    assert obj.kind is not None and obj.kind.device_class == "motion"
    assert obj.value == "1"


def test_symulacja_classifies_but_is_not_bridged() -> None:
    client = _client()
    client._feed_message(f"ampio/fromDB/{USER}/config/devices", _devices(_PANEL))
    sym = {
        "id": 61,
        "id_urzadzenia": 7,
        "typ_komponentu": "symulacja",
        "interpretacja": 1,
        "funkcja": 1,
        "opis_menu": "Sim",
    }
    client._feed_message(f"ampio/fromDB/{USER}/config/devicesDetails", _details(sym))
    assert client.objects[61].is_input is True
    assert client._store._input_index == {}  # symulacja prefix not bridged


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


def test_numeric_value_none_for_bare_nan_state_push() -> None:
    """A bare NaN literal parses (Python's json accepts it) but reads as None."""
    client = _client()
    client._feed_message(f"ampio/fromDB/{USER}/ob/12/state", b'{"state": NaN}')
    obj = client.objects[12]
    assert obj.value == "nan"
    assert obj.numeric_value is None


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


# --- app-sync data-surface fallback (non-admin accounts) --------------------

DATA_DEVICES_TOPIC = f"ampio/fromDB/{USER}/data/devices"
PARAMS_DEVICES_TOPIC = f"ampio/fromDB/{USER}/data/params_devices"


def _app_row(oid: int, leaf: str, name: str = "Air quality", interp: int = 5) -> dict:
    """One `data/devices` row: the devicesDetails shape minus params/stan_json."""
    return {
        "id": oid,
        "id_urzadzenia": 20,
        "typ_komponentu": "lin_wej",
        "interpretacja": interp,
        "funkcja": 5,
        "leafId": leaf,
        "opis_menu": name,
    }


def test_data_devices_populate_and_classify() -> None:
    client = _client()
    client._feed_message(
        DATA_DEVICES_TOPIC, _devices(_app_row(24, "0_cb9b_74_0_1", interp=7))
    )
    obj = client.objects[24]
    assert obj.name == "Air quality"
    assert obj.kind is not None and obj.kind.device_class == "carbon_dioxide"
    assert obj.device_id == 20 and obj.funkcja == 5
    assert obj.leaf_id == "0_cb9b_74_0_1"


def test_params_table_before_catalogue_supplies_hidden_flag() -> None:
    """A params table that arrives first is applied when the catalogue lands."""
    client = _client()
    client._feed_message(
        PARAMS_DEVICES_TOPIC,
        _devices({"id": 24, "params": 17}, {"id": 25, "params": 1}),
    )
    # The table is not grant-filtered; unknown ids create no placeholders.
    assert client.objects == {}

    client._feed_message(
        DATA_DEVICES_TOPIC,
        _devices(_app_row(24, "0_cb9b_74_0_1"), _app_row(25, "0_cb9b_74_0_2")),
    )
    assert client.objects[24].hidden is True and client.objects[24].visible is False
    assert client.objects[25].hidden is False and client.objects[25].visible is True


def test_params_table_after_catalogue_updates_objects_and_notifies() -> None:
    client = _client()
    client._feed_message(DATA_DEVICES_TOPIC, _devices(_app_row(24, "0_cb9b_74_0_1")))
    received: list = []
    client.add_object_listener(received.append)

    client._feed_message(
        PARAMS_DEVICES_TOPIC,
        _devices({"id": 24, "params": 17}, {"id": 999, "params": 1}),
    )
    assert client.objects[24].hidden is True
    assert received == [client.objects[24]]
    assert 999 not in client.objects


def test_data_devices_does_not_degrade_details() -> None:
    """On the admin tier both catalogues arrive; the poorer one must not clobber."""
    client = _client()
    row = _app_row(24, "0_cb9b_74_0_1", name="Named")
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details({**row, "params": (1 << 37) | 1}),
    )
    client._feed_message(DATA_DEVICES_TOPIC, _devices(row))
    obj = client.objects[24]
    assert obj.params == (1 << 37) | 1
    assert obj.name == "Named"


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

        async def publish(self, _topic, _payload=b""):
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


# --- cover tilt state ------------------------------------------------------


def test_lammel_is_parsed_into_tilt_position() -> None:
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details({"id": 66, "typ_komponentu": "roleta_lamelki", "interpretacja": 1}),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/ob/66/state",
        b'{ "state": "95","lammel": "65","block": "0" , "on": 1786723383804}',
    )
    obj = client.objects[66]
    assert obj.value == "95"
    assert obj.tilt_position == 65
    assert obj.supports_tilt is True
    assert obj.is_output is True


def test_plain_cover_reports_no_tilt() -> None:
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details({"id": 48, "typ_komponentu": "roleta_procenty", "interpretacja": 1}),
    )
    client._feed_message(
        f"ampio/fromDB/{USER}/ob/48/state", b'{ "state": "55","block": "0" }'
    )
    obj = client.objects[48]
    assert obj.value == "55"
    assert obj.tilt_position is None
    assert obj.supports_tilt is False


def test_states_snapshot_seeds_tilt_position() -> None:
    client = _client()
    client._feed_message(
        f"ampio/fromDB/{USER}/data/states",
        _states(
            {
                "id": 66,
                "stan_json": '{"state": "100", "lammel": "100", "on": 1779560000000}',
            }
        ),
    )
    assert client.objects[66].tilt_position == 100


# --- module diagnostics ----------------------------------------------------


def _diag_client() -> AmpioClient:
    """Client that knows module 7 at mac 0xCAFE."""
    client = _client()
    client._feed_message(f"ampio/fromDB/{USER}/config/devices", _devices(_PANEL))
    return client


def test_diagnostics_sets_voltage_and_temperature() -> None:
    client = _diag_client()
    seen: list = []
    client.add_module_listener(seen.append)

    client._feed_message("ampio/from/CAFE/b/4F", b'{"d":[254,79,63,142],"m":51966}')

    module = client.modules[7]
    assert module.supply_voltage == 12.6
    assert module.temperature == 42.0
    assert module.last_seen is not None
    assert seen == [module]


def test_diagnostics_without_a_temperature_sensor_reports_none() -> None:
    """`0` in the temperature byte marks the sensor as absent, not -100 C."""
    client = _diag_client()
    client._feed_message("ampio/from/CAFE/b/4F", b'{"d":[254,79,60,0],"m":51966}')
    module = client.modules[7]
    assert module.supply_voltage == 12.0
    assert module.temperature is None


def test_diagnostics_for_an_unknown_module_is_ignored() -> None:
    client = _diag_client()
    client._feed_message("ampio/from/BEEF/b/4F", b'{"d":[254,79,60,0],"m":48879}')
    assert client.modules[7].supply_voltage is None


@pytest.mark.parametrize(
    "payload",
    [
        b'{"d":[254,80,60,0]}',  # not the diagnostics frame type
        b'{"d":[1,79,60,0]}',  # not a broadcast
        b'{"d":[254,79]}',  # truncated
        b"not json",
    ],
)
def test_non_diagnostics_frames_are_ignored(payload: bytes) -> None:
    client = _diag_client()
    client._feed_message("ampio/from/CAFE/b/4F", payload)
    assert client.modules[7].supply_voltage is None
