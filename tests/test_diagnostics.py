"""Retained reply summaries omit private payload content."""

import json

import pytest
from conftest import (
    ADMIN_INFO_TOPIC,
    details,
    devices,
    feed,
    info,
    params_table,
    rows,
    snapshot,
)

from ampio_mqtt import AmpioAdminClient, AmpioClient
from ampio_mqtt._protocol import REDACTED


def _client_for(username: str) -> AmpioClient:
    """The client class the account gets, as a consumer would pick it."""
    if username == "admin":
        return AmpioAdminClient("host")
    return AmpioClient("host", username=username)


_URL = "https://example.invalid/private-url-token"


# Each case carries two rows the surface's parser accepts, so the retained
# summary is compared against a reply that actually reached the store. Every
# column that can hold a user-given string holds a `private-` marker.
@pytest.mark.parametrize(
    ("endpoint", "surface", "username", "payload"),
    [
        (
            "devices",
            "config/devices",
            "admin",
            devices(
                {"id": 1, "mac": 10, "nazwa_urzadzenia": "private-module-name"},
                {"id": 2, "mac": 11, "nazwa_urzadzenia": "private-second-module"},
            ),
        ),
        (
            "states",
            "data/states",
            "admin",
            snapshot(
                {
                    "id": 1,
                    "stan_json": json.dumps(
                        {"state": "private-state-text", "on": 1779560000000}
                    ),
                },
                {
                    "id": 2,
                    "stan_json": json.dumps(
                        {"state": "private-second-state", "on": 1779560000000}
                    ),
                },
            ),
        ),
        (
            "data_devices",
            "data/devices",
            "u",
            details(
                {"id": 1, "opis_menu": "private-object-name", "url": _URL},
                {"id": 2, "opis_menu": "private-second-object", "url": _URL},
            ),
        ),
        (
            "params_devices",
            "data/params_devices",
            "u",
            params_table({"id": 1, "url": _URL}, {"id": 2, "url": _URL}),
        ),
        (
            "groups",
            "data/groups",
            "admin",
            rows(
                {"id": 8, "id_rodzica": 4, "opis_menu": "private-room-name"},
                {"id": 9, "id_rodzica": 4, "opis_menu": "private-second-room"},
            ),
        ),
        (
            "group_devices",
            "data/group_devices",
            "admin",
            rows({"id_grupy": 8, "id_obiektu": 31}, {"id_grupy": 9, "id_obiektu": 32}),
        ),
        (
            "scenes",
            "data/scenes",
            "admin",
            rows(
                {
                    "id": 3,
                    "parentId": -1,
                    "active": 1,
                    "sceneName": "private-scene",
                    "Infos": [{"id": 31}],
                },
                {
                    "id": 4,
                    "parentId": 8,
                    "active": 0,
                    "sceneName": "private-scene-2",
                    "Infos": [{"id": 32}],
                },
            ),
        ),
        (
            "locations",
            "config/locations",
            "admin",
            rows(
                {"id": 1, "opis_menu": "private-location-name"},
                {"id": 2, "opis_menu": "private-second-location"},
            ),
        ),
    ],
)
def test_retained_reply_omits_private_keys_and_values(
    endpoint: str, surface: str, username: str, payload: str
) -> None:
    client = _client_for(username)
    feed(client, f"ampio/fromDB/{username}/{surface}", payload)
    report = client.diagnostics_snapshot()
    # A refused row reaches no store, which would let the privacy assertion
    # below pass on a snapshot that holds nothing.
    assert report["connection"]["protocol_violations"] == {}
    assert json.loads(report["last_payloads"][endpoint]) == {"row_count": 2}
    assert "private-" not in json.dumps(report)


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
    client = AmpioAdminClient("host")
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


def test_the_report_writes_every_module_mac_as_an_address() -> None:
    """Both module entries carry the hex form a reader meets elsewhere."""
    client = AmpioAdminClient("host")
    feed(client, ADMIN_INFO_TOPIC, info(mac=47846, userId=-1))
    feed(
        client,
        "ampio/fromDB/admin/config/devices",
        devices(
            {"id": 1, "mac": 0xCB8F},
            {"id": 2, "mac": 0xBE82},
            {"id": 3, "mac": 0xBE82},
        ),
    )
    report = client.diagnostics_snapshot()
    assert report["modules"][0]["mac"] == "0xCB8F"
    assert report["mac_collisions"] == [["0xBE82", [2, 3]]]
    # The server's own row is the decimal `server_key` a consumer holds as
    # its registry id, and this block is the dataclass as it stands.
    assert report["server_info"]["mac"] == 47846
