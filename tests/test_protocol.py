"""Direct tests for the pure parsers in `aioampio._protocol`."""

from __future__ import annotations

import json

import aiomqtt
import pytest

from aioampio import AmpioModule, AmpioServerInfo
from aioampio._protocol import (
    is_auth_error,
    parse_details,
    parse_devices,
    parse_server_info,
    parse_stan_json,
    parse_state_message,
    parse_states_snapshot,
    to_int,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        ("2", 2),
        (3.7, 3),
        (None, None),
        ("not-a-number", None),
        ("", None),
    ],
)
def test_to_int(value: object, expected: int | None) -> None:
    assert to_int(value) == expected


@pytest.mark.parametrize(
    "marker",
    [
        "not authorized",
        "Bad user name or password",
        "[code:5] unauthorized",
        "Connection refused: rc=4",
    ],
)
def test_is_auth_error_true(marker: str) -> None:
    assert is_auth_error(aiomqtt.MqttError(marker))


@pytest.mark.parametrize(
    "marker",
    [
        "connection refused",
        "timeout",
        "host unreachable",
    ],
)
def test_is_auth_error_false(marker: str) -> None:
    assert not is_auth_error(aiomqtt.MqttError(marker))


def test_parse_details_returns_metadata() -> None:
    payload = json.dumps(
        {
            "List": [
                {
                    "id": 41,
                    "id_urzadzenia": 3,
                    "typ_komponentu": "temp",
                    "interpretacja": 1,
                    "opis_menu": "Salon",
                    "stan_json": json.dumps({"state": "21.5", "on": 1700000000000}),
                },
                {"id": "bad"},  # skipped: non-int id
                {"id": 42},  # kept: minimal record
            ]
        }
    )
    items = parse_details(payload)
    assert items is not None
    assert [m.id for m in items] == [41, 42]
    assert items[0].name == "Salon"
    assert items[0].stan_json is not None
    assert items[1].name is None and items[1].stan_json is None


def test_parse_details_bad_json() -> None:
    assert parse_details("not json") is None


def test_parse_devices_bad_json() -> None:
    assert parse_devices("not json") is None


def test_parse_devices_skips_non_int_id() -> None:
    payload = json.dumps({"List": [{"id": "x"}, {"id": 5, "typ_urzadzenia": 1}]})
    modules = parse_devices(payload)
    assert modules is not None and [m.id for m in modules] == [5]


def test_parse_states_snapshot_bad_json() -> None:
    assert parse_states_snapshot("not json") is None


def test_parse_state_message_non_dict_payload() -> None:
    """A JSON array payload falls through to text-mode and yields the raw string."""
    update = parse_state_message("ampio/fromDB/u/ob/41/state", json.dumps([1, 2]))
    assert update is not None
    assert update.value == "[1, 2]" and update.on_ms is None


def test_parse_devices_returns_modules() -> None:
    payload = json.dumps(
        {
            "List": [
                {
                    "id": 3,
                    "mac": 10,
                    "mac_global": 1234,
                    "nazwa_urzadzenia": "M-SENS",
                    "typ_urzadzenia": 5,
                    "wersja_softu": 7,
                    "wersja_pcb": 1,
                },
            ]
        }
    )
    modules = parse_devices(payload)
    assert modules is not None
    [module] = modules
    assert isinstance(module, AmpioModule)
    assert module.id == 3 and module.last_seen is None


def test_parse_server_info_extracts_safe_fields() -> None:
    payload = json.dumps(
        {
            "Results": {
                "mac": 1234,
                "serverVersion": "3.4.5",
                "local_ip": "192.168.1.10",
                "secretToken": "ignored",
            }
        }
    )
    info = parse_server_info(payload)
    assert info.mac == 1234
    assert info.server_version == "3.4.5"
    assert info.local_ip == "192.168.1.10"


def test_parse_server_info_bad_payload_returns_empty() -> None:
    assert parse_server_info("not json") == AmpioServerInfo()
    assert parse_server_info(json.dumps([1, 2, 3])) == AmpioServerInfo()


def test_parse_states_snapshot() -> None:
    payload = json.dumps(
        {"List": [{"id": 1, "stan_json": '{"state":"on"}'}, {"id": "x"}]}
    )
    entries = parse_states_snapshot(payload)
    assert entries is not None
    assert len(entries) == 1
    assert entries[0].id == 1 and entries[0].stan_json is not None


def test_parse_state_message_json_payload() -> None:
    update = parse_state_message(
        "ampio/fromDB/u/ob/41/state", json.dumps({"state": "22.4", "on": 1700})
    )
    assert update is not None
    assert update.id == 41 and update.value == "22.4" and update.on_ms == 1700


def test_parse_state_message_plain_payload() -> None:
    update = parse_state_message("ampio/fromDB/u/ob/41/state", "ok")
    assert update is not None
    assert update.value == "ok" and update.on_ms is None


@pytest.mark.parametrize(
    "topic",
    [
        "ampio/fromDB/u/notob/41/state",
        "too/short",
        "ampio/fromDB/u/ob/notanint/state",
    ],
)
def test_parse_state_message_invalid_topic(topic: str) -> None:
    assert parse_state_message(topic, "x") is None


def test_parse_stan_json_extracts_value_and_timestamp() -> None:
    seed = parse_stan_json(json.dumps({"state": "21.0", "on": 1700000000000}))
    assert seed is not None
    assert seed.value == "21.0" and seed.on_ms == 1700000000000


@pytest.mark.parametrize("payload", ["", "not json", json.dumps([1, 2])])
def test_parse_stan_json_invalid(payload: str) -> None:
    assert parse_stan_json(payload) is None
