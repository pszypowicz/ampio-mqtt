"""Tests for AmpioClient DB-object message handling (no real broker)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from aioampio import AmpioClient

USER = "u"


def _msg(topic: str, payload: bytes) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, payload=payload)


def _details(*items) -> bytes:
    return json.dumps({"Status": 0, "List": list(items)}).encode()


def _devices(*items) -> bytes:
    return json.dumps({"List": list(items)}).encode()


def _client() -> AmpioClient:
    return AmpioClient("host", username=USER)


def test_details_populate_and_classify() -> None:
    client = _client()
    payload = _details(
        {"id": 41, "id_urzadzenia": 3, "typ_komponentu": "temp", "funkcja": 1,
         "interpretacja": 1, "opis_menu": "Salon", "lokalizacja": 2, "min": 0, "max": 50},
        {"id": 107, "id_urzadzenia": 3, "typ_komponentu": "lin_wej", "funkcja": 7,
         "interpretacja": 7, "opis_menu": "CO2", "lokalizacja": 2},
        {"id": 1, "id_urzadzenia": 1, "typ_komponentu": "przekaznik", "funkcja": 1,
         "interpretacja": 1, "opis_menu": "Pump", "lokalizacja": 0},
    )
    client._handle_message(_msg(f"ampio/fromDB/{USER}/config/devicesDetails", payload))

    assert set(client.objects) == {41, 107, 1}
    temp = client.objects[41]
    assert temp.kind is not None and temp.kind.device_class == "temperature"
    assert temp.name == "Salon" and temp.device_id == 3 and temp.room_id == 2
    # relay is not a sensor
    assert client.objects[1].is_sensor is False
    assert set(client.sensors) == {41, 107}


def test_devices_populate_modules_with_model_and_versions() -> None:
    client = _client()
    payload = _devices(
        {"id": 17, "mac": 52111, "typ_urzadzenia": 44,
         "nazwa_urzadzenia": "m-sens salon", "wersja_softu": 63, "wersja_pcb": 7},
        {"id": 99, "mac": 1, "typ_urzadzenia": 999,
         "nazwa_urzadzenia": "Mystery", "wersja_softu": 1, "wersja_pcb": 2},
    )
    client._handle_message(_msg(f"ampio/fromDB/{USER}/config/devices", payload))

    mod = client.modules[17]
    assert mod.name == "m-sens salon"
    assert mod.type == 44
    assert mod.model == "M-SENS"
    assert mod.sw_version == 63
    assert mod.hw_version == 7
    # Unknown type code -> no model name, but the module is still tracked.
    assert client.modules[99].model is None


def test_state_updates_object_and_notifies() -> None:
    client = _client()
    client._handle_message(
        _msg(
            f"ampio/fromDB/{USER}/config/devicesDetails",
            _details({"id": 41, "typ_komponentu": "temp", "interpretacja": 1,
                      "opis_menu": "Salon"}),
        )
    )
    received: list = []
    client.add_object_listener(received.append)

    client._handle_message(
        _msg(
            f"ampio/fromDB/{USER}/ob/41/state",
            b'{ "state": "22.5","desc": "22.5 C" , "on": 1779555459594} ',
        )
    )
    obj = client.objects[41]
    assert obj.value == "22.5"
    assert obj.desc == "22.5 C"
    assert received == [obj]


def test_state_without_metadata_creates_generic_sensor() -> None:
    client = _client()
    client._handle_message(
        _msg(f"ampio/fromDB/{USER}/ob/93/state", b'{"state":"187.6","desc":"187.6 "}')
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
