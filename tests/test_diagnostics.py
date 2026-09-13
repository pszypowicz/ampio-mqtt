"""Retained reply summaries omit private payload content."""

import json

import pytest
from conftest import devices, feed, rows

from ampio_mqtt import AmpioClient
from ampio_mqtt._protocol import REDACTED


@pytest.mark.parametrize(
    ("endpoint", "surface"),
    [
        ("devices", "config/devices"),
        ("details", "config/devicesDetails"),
        ("states", "data/states"),
        ("data_devices", "data/devices"),
        ("params_devices", "data/params_devices"),
        ("groups", "data/groups"),
        ("group_devices", "data/group_devices"),
        ("scenes", "data/scenes"),
        ("locations", "config/locations"),
    ],
)
def test_retained_reply_omits_private_keys_and_values(
    endpoint: str, surface: str
) -> None:
    username = "u" if endpoint in {"data_devices", "params_devices"} else "admin"
    client = AmpioClient("host", username=username)
    payload = json.dumps(
        {
            "private-envelope-token": "private-envelope-value",
            "List": [
                {
                    "id": 1,
                    "nazwa_urzadzenia": "private-module-name",
                    "opis_menu": "private-room-name",
                    "sceneName": "private-scene-name",
                    "name": "private-location-name",
                    "url": "https://example.invalid/private-url-token",
                    "stan_json": json.dumps(
                        {
                            "state": "private-state-description",
                            "private-key": ["secret"],
                        }
                    ),
                    "private-row-key": {"nested": "private-nested-value"},
                },
                {},
            ],
        }
    )
    feed(client, f"ampio/fromDB/{username}/{surface}", payload)
    retained = client.diagnostics_snapshot()["last_payloads"][endpoint]
    assert json.loads(retained) == {"row_count": 2}
    assert "private-" not in json.dumps(client.diagnostics_snapshot())


@pytest.mark.parametrize(
    "payload",
    [
        '{"List": [{"name": "private-truncated',
        '"private-text"',
        '[{"name": "private-name"}]',
        '{"List": "private-value"}',
        '{"List": ["private-value"]}',
        '{"private-key": "private-value"}',
    ],
)
def test_retained_malformed_reply_withholds_all_bytes(payload: str) -> None:
    client = AmpioClient("host", username="u")
    feed(client, "ampio/fromDB/u/data/groups", payload)
    assert client.diagnostics_snapshot()["last_payloads"]["groups"] == REDACTED
    assert "private-" not in json.dumps(client.diagnostics_snapshot())


def test_retained_summary_distinguishes_empty_reply_from_missing_reply() -> None:
    client = AmpioClient("host", username="u")
    assert "groups" not in client.diagnostics_snapshot()["last_payloads"]
    feed(client, "ampio/fromDB/u/data/groups", rows())
    assert json.loads(client.diagnostics_snapshot()["last_payloads"]["groups"]) == {
        "row_count": 0
    }


def test_summary_preserves_module_diagnostics_and_live_names() -> None:
    client = AmpioClient("host", username="admin")
    feed(
        client,
        "ampio/fromDB/admin/config/devices",
        devices({"id": 1, "nazwa_urzadzenia": "private-module-name"}),
    )
    assert client.modules[1].nazwa_urzadzenia == "private-module-name"
    snapshot = client.diagnostics_snapshot()
    assert snapshot["modules"][0]["id"] == 1
    assert snapshot["modules"][0]["typ_urzadzenia"] == 44
    assert "private-module-name" not in json.dumps(snapshot)
