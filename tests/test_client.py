"""Tests for AmpioClient DB-object message handling (no real broker)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import fields
from unittest.mock import patch

import aiomqtt
import pytest

from aioampio import AmpioAuthError, AmpioClient

USER = "u"


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
    client.feed_message(
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
    # relay is not a sensor
    assert client.objects[1].is_sensor is False
    assert set(client.sensors) == {41, 107}


def test_devices_populate_modules_with_model_and_versions() -> None:
    client = _client()
    client.feed_message(
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


def test_state_updates_module_last_seen_from_on_field() -> None:
    client = _client()
    client.feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client.feed_message(
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

    client.feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state": "22.5", "on": 1779565263813}',
    )
    # 1779565263813 ms -> 1779565263.813 s
    assert client.modules[17].last_seen == 1779565263.813

    # A later push moves last_seen forward; an older one does not regress it.
    client.feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state": "23.0", "on": 1779565999000}',
    )
    assert client.modules[17].last_seen == 1779565999.0
    client.feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state": "21.0", "on": 1779560000000}',
    )
    assert client.modules[17].last_seen == 1779565999.0


def test_states_snapshot_seeds_value_and_last_seen() -> None:
    """The bulk states reply seeds value and bumps module last_seen."""
    client = _client()
    client.feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client.feed_message(
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

    client.feed_message(
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
    assert client.modules[17].last_seen == 1779560000.0


def test_states_snapshot_does_not_overwrite_live_value() -> None:
    """A snapshot does not regress a value already set by a live push."""
    client = _client()
    client.feed_message(
        f"ampio/fromDB/{USER}/config/devicesDetails",
        _details(
            {"id": 41, "typ_komponentu": "temp", "interpretacja": 1, "opis_menu": "T"}
        ),
    )
    client.feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{"state": "fresh", "on": 1779570000000}',
    )
    assert client.objects[41].value == "fresh"

    client.feed_message(
        f"ampio/fromDB/{USER}/data/states",
        _states({"id": 41, "stan_json": '{"state": "stale", "on": 1779560000000}'}),
    )
    assert client.objects[41].value == "fresh"


def test_states_snapshot_creates_placeholder_for_unknown_object() -> None:
    """A state for an object whose metadata is not yet known is still tracked."""
    client = _client()
    client.feed_message(
        f"ampio/fromDB/{USER}/data/states",
        _states({"id": 999, "stan_json": '{"state": "1", "on": 1779560000000}'}),
    )
    assert client.objects[999].value == "1"
    # The kind is the generic fallback because no metadata existed.
    assert client.objects[999].kind is not None
    assert client.objects[999].kind.key == "value"


def test_details_seeds_module_last_seen_from_stan_json() -> None:
    """The `on` timestamp inside stan_json seeds the module's last_seen."""
    client = _client()
    client.feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client.feed_message(
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
    assert client.modules[17].last_seen == 1779560000.0


def test_info_parses_only_safe_fields() -> None:
    """Server info parsing keeps version/ip/mac but drops geo/cloud/private fields."""
    client = _client()
    client.feed_message(
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
    client.feed_message(
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
    client.feed_message(
        f"ampio/fromDB/{USER}/data/info",
        _info(serverVersion="1", mac="47846"),
    )
    assert client.mserv_id == 1


def test_mserv_id_falls_back_to_typ10_without_info() -> None:
    """Without info, a unique typ_urzadzenia=10 module identifies the M-SERV."""
    client = _client()
    client.feed_message(
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
    client.feed_message(
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
    client.feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m"}),
    )
    client.state.modules[17].last_seen = 1700000000.0
    # Re-deliver the devices list (e.g. on reconnect) - last_seen must persist.
    client.feed_message(
        f"ampio/fromDB/{USER}/config/devices",
        _devices({"id": 17, "mac": 1, "typ_urzadzenia": 44, "nazwa_urzadzenia": "m2"}),
    )
    assert client.modules[17].name == "m2"
    assert client.modules[17].last_seen == 1700000000.0


def test_state_updates_object_and_notifies() -> None:
    client = _client()
    client.feed_message(
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

    client.feed_message(
        f"ampio/fromDB/{USER}/ob/41/state",
        b'{ "state": "22.5","desc": "22.5 C" , "on": 1779555459594} ',
    )
    obj = client.objects[41]
    assert obj.value == "22.5"
    assert received == [obj]


def test_state_without_metadata_creates_generic_sensor() -> None:
    client = _client()
    client.feed_message(
        f"ampio/fromDB/{USER}/ob/93/state",
        b'{"state":"187.6","desc":"187.6 "}',
    )
    obj = client.objects[93]
    assert obj.is_sensor is True  # generic fallback
    assert obj.value == "187.6"


def test_availability_listener() -> None:
    client = _client()
    events: list[bool] = []
    client.add_availability_listener(events.append)
    client._set_available(True)
    client._set_available(True)
    client._set_available(False)
    assert events == [True, False]


class _AuthFailingClient:
    """aiomqtt.Client stand-in whose context manager raises an auth error."""

    def __init__(self, *args, **kwargs) -> None:
        self.messages = self  # any iterable - won't be reached

    async def __aenter__(self):
        raise aiomqtt.MqttError("Not authorized")

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def test_start_raises_auth_error_on_credential_rejection() -> None:
    """A broker auth rejection during _run surfaces AmpioAuthError from start()."""
    client = AmpioClient("host", username="u", password="bad", reconnect_interval=0.0)
    with (
        patch("aioampio.client.aiomqtt.Client", _AuthFailingClient),
        pytest.raises(AmpioAuthError),
    ):
        await client.start(timeout=2.0, discovery_timeout=0.1)
    assert client._runner is None  # stop() ran during the raise


@pytest.mark.parametrize(
    "topic_suffix",
    ["config/devicesDetails", "config/devices", "data/states"],
)
def test_handlers_log_and_skip_unparseable_payloads(
    caplog: pytest.LogCaptureFixture, topic_suffix: str
) -> None:
    client = _client()
    with caplog.at_level("WARNING", logger="aioampio.client"):
        client.feed_message(f"ampio/fromDB/{USER}/{topic_suffix}", b"not json")
    assert "Could not parse" in caplog.text


def test_dispatch_ignores_unmatched_topics() -> None:
    """A topic that matches none of the four patterns is silently ignored."""
    client = _client()
    client.feed_message("totally/unrelated/topic", b"anything")
    assert client.objects == {}
    assert client.modules == {}


def test_state_with_unparseable_payload_is_dropped() -> None:
    """An `/ob/<non-int>/state` topic is rejected without raising."""
    client = _client()
    client.feed_message(f"ampio/fromDB/{USER}/ob/not-an-int/state", b"x")
    assert client.objects == {}


def test_stan_json_with_no_state_field_does_not_overwrite_value() -> None:
    """A stan_json blob without `state` should not clobber an existing value."""
    client = _client()
    client.feed_message(
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
    from aioampio import AmpioConnectionError

    with (
        patch("aioampio.client.aiomqtt.Client", _Stuck),
        pytest.raises(AmpioConnectionError),
    ):
        await client.start(timeout=0.5, discovery_timeout=0.1)
