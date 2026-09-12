"""Tests for the probe scripts in tools/.

Each script's ``run()`` takes a client factory, so the shared FakeBroker
drives it end to end without a broker. The scripts import as top-level
modules through the ``pythonpath`` entry in the pytest configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path
from types import ModuleType

import aiomqtt
import dump
import modules
import pytest
import set_object
import smoke_test
from conftest import (
    ADMIN_DETAILS_TOPIC,
    ADMIN_DEVICES_TOPIC,
    ADMIN_INFO_TOPIC,
    ADMIN_STATES_TOPIC,
    ADMIN_USER,
    DATA_DEVICES_TOPIC,
    INFO_TOPIC,
    PARAMS_DEVICES_TOPIC,
    STATES_TOPIC,
    USER,
    FakeBroker,
    Message,
    details,
    devices,
    info,
    params_table,
    snapshot,
)

from ampio_mqtt import AmpioClient, ModuleFunction
from ampio_mqtt._protocol import (
    DEVICE_API_LIST_PAYLOAD,
    DEVICE_API_LIST_REQUEST,
    DEVICE_API_LIST_TOPIC,
)

API_TOPIC = f"ampio/control/{USER}/api"
ADMIN_CONFIG_REQUEST = f"ampio/control/{ADMIN_USER}/config"
LOCATIONS_TOPIC = f"ampio/fromDB/{ADMIN_USER}/config/locations"


def _parse(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *argv: str,
    user: str = USER,
) -> argparse.Namespace:
    """The script's own ``parse_args`` over ``argv``, credentials filled."""
    monkeypatch.setattr(
        sys,
        "argv",
        [module.__name__, "--host", "h", "--username", user, "--password", "p", *argv],
    )
    return module.parse_args()


def _discovery(*rows: dict) -> list[Message]:
    """The restricted tier's four initial replies, so ``connect()`` completes."""
    return [
        Message(DATA_DEVICES_TOPIC, details(*rows).encode()),
        Message(
            PARAMS_DEVICES_TOPIC,
            params_table(*({"id": r["id"]} for r in rows)).encode(),
        ),
        Message(STATES_TOPIC, snapshot().encode()),
        Message(INFO_TOPIC, info(mac=1, userId="7").encode()),
    ]


# --- dump.py ----------------------------------------------------------------


def test_dump_client_ids_differ_per_run() -> None:
    """Two captures at once must not share a client id: the broker kicks one."""
    first, second = dump.client_id(), dump.client_id()
    assert first != second
    assert first.startswith("ampio_mqtt_dump")


async def test_dump_subscribes_before_it_publishes_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = FakeBroker()
    broker.scripted_messages = [Message("device_api/from/list", b"[]")]
    a = _parse(
        monkeypatch,
        dump,
        "--topic",
        "device_api/from/list",
        "--request",
        "device_api/to/list",
        "--request-payload",
        "0",
        "--max",
        "1",
    )
    assert await dump.run(a, client_factory=broker.factory) == 0
    assert broker.log == [
        ("subscribe", "device_api/from/list"),
        ("publish", "device_api/to/list"),
    ]
    assert broker.published == [("device_api/to/list", b"0")]
    assert broker.subscribed_qos == [1]


async def test_dump_max_zero_means_no_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()
    broker.scripted_messages = [Message(f"t/{n}", b"x") for n in range(3)]
    a = _parse(monkeypatch, dump, "--topic", "t/#", "--duration", "0.05")
    assert a.max == 0
    assert await dump.run(a, client_factory=broker.factory) == 0
    assert "Total messages received: 3 (0 retained)" in capsys.readouterr().out


async def test_dump_stops_at_max_before_the_duration_ends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()
    broker.scripted_messages = [Message(f"t/{n}", b"x") for n in range(3)]
    a = _parse(monkeypatch, dump, "--topic", "t/#", "--duration", "30", "--max", "2")
    async with asyncio.timeout(1.0):
        assert await dump.run(a, client_factory=broker.factory) == 0
    assert "Total messages received: 2 (0 retained)" in capsys.readouterr().out


async def test_dump_marks_retained_replays_and_writes_the_outfile(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    broker = FakeBroker()
    broker.retained = {"ampio/from/CAFE/state/f/3": b"1"}
    broker.scripted_messages = [Message("ampio/from/CAFE/state/f/4", b"0")]
    outfile = tmp_path / "sweep.tsv"
    a = _parse(
        monkeypatch,
        dump,
        "--topic",
        "ampio/from/+/state/f/+",
        "--qos",
        "0",
        "--max",
        "2",
        "--outfile",
        str(outfile),
    )
    assert await dump.run(a, client_factory=broker.factory) == 0
    assert broker.subscribed_qos == [0]
    printed = capsys.readouterr().out
    assert " R ampio/from/CAFE/state/f/3  =  1" in printed
    assert "   ampio/from/CAFE/state/f/4  =  0" in printed
    assert "Total messages received: 2 (1 retained)" in printed
    assert sorted(outfile.read_text().splitlines()) == [
        " \tampio/from/CAFE/state/f/4\t0",
        "R\tampio/from/CAFE/state/f/3\t1",
    ]


async def test_dump_reports_a_broker_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()
    broker.enter_errors = [aiomqtt.MqttError("boom")]
    a = _parse(monkeypatch, dump, "--topic", "t/#")
    assert await dump.run(a, client_factory=broker.factory) == 1
    assert "MQTT error: boom" in capsys.readouterr().out


# --- set_object.py ----------------------------------------------------------


async def test_set_object_sends_the_command_and_reports_the_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()
    broker.scripted_messages = [
        *_discovery({"id": 64, "typ_komponentu": "flaga", "opis_menu": "Flag"}),
        Message(
            f"ampio/fromDB/{USER}/ob/64/state", b'{"state":"0","on":1789000000000}'
        ),
    ]
    a = _parse(monkeypatch, set_object, "--object-id", "64", "--on", "--watch", "0.01")
    assert await set_object.run(a, client_factory=broker.factory) == 0
    assert (API_TOPIC, b"/api/set/64/turnOn") in broker.published
    printed = capsys.readouterr().out
    assert "  state  ob/64 = 0" in printed
    assert "before: ob/64 = 0" in printed
    assert "after:  ob/64 = 0" in printed


async def test_set_object_passes_a_raw_verb_and_its_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = FakeBroker()
    broker.scripted_messages = _discovery()
    a = _parse(
        monkeypatch,
        set_object,
        "--object-id",
        "135",
        "--verb",
        "setValue",
        "--arg",
        "255",
        "--watch",
        "0.01",
    )
    assert await set_object.run(a, client_factory=broker.factory) == 0
    assert (API_TOPIC, b"/api/set/135/setValue/255") in broker.published


async def test_set_object_rejects_a_color_without_four_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = _parse(monkeypatch, set_object, "--object-id", "50", "--color", "1,2,3")
    with pytest.raises(SystemExit):
        await set_object.send(AmpioClient("h", username=USER), a)


# --- smoke_test.py ----------------------------------------------------------


async def test_smoke_test_prints_the_discovery_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()
    broker.scripted_messages = [
        *_discovery(
            {
                "id": 41,
                "typ_komponentu": "temp",
                "interpretacja": 1,
                "opis_menu": "Salon",
            }
        ),
        Message(
            f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"22.5","on":1789000000000}'
        ),
    ]
    a = _parse(monkeypatch, smoke_test, "--duration", "0.01")
    assert await smoke_test.run(a, client_factory=broker.factory) == 0
    printed = capsys.readouterr().out
    assert "  state  ob/41" in printed and "= 22.5" in printed
    assert "=== Objects: 1 (sensors: 1), modules: 0 ===" in printed
    assert "Salon" in printed


async def test_smoke_test_reports_a_failed_connect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()
    broker.enter_errors = [RuntimeError("boom")]
    a = _parse(monkeypatch, smoke_test, "--duration", "0.01")
    assert await smoke_test.run(a, client_factory=broker.factory) == 1
    assert "FAILED to connect: Connection loop died: boom" in capsys.readouterr().out


# --- modules.py -------------------------------------------------------------

PANEL = {
    "id": 16,
    "mac": 0xCB89,
    "typ_urzadzenia": 8,  # M-DOT-4
    "wersja_softu": 42,
    "nazwa_urzadzenia": "Sypialnia",
}
RELAY = {"id": 17, "mac": 0xBEEF, "typ_urzadzenia": 15}  # M-IN-8s


def _admin_discovery(*rows: dict) -> list[Message]:
    """The admin tier's four initial replies, so ``connect()`` completes."""
    return [
        Message(ADMIN_DETAILS_TOPIC, details().encode()),
        Message(ADMIN_DEVICES_TOPIC, devices(*rows).encode()),
        Message(ADMIN_STATES_TOPIC, snapshot().encode()),
        Message(ADMIN_INFO_TOPIC, info(mac=1, userId="-1").encode()),
    ]


def _caps(*pairs: tuple[int, int]) -> str:
    """A ``supportedFunctions`` blob: 2 bytes per (function id, channel count)."""
    return base64.b64encode(bytes(b for pair in pairs for b in pair)).decode()


def _device_list(*devices_caps: tuple[int, str]) -> str:
    """A ``device_api/from/list`` reply carrying one entry per (mac, blob)."""
    return json.dumps(
        {
            "devices": [
                {"macProd": mac, "macUser": mac, "supportedFunctions": blob}
                for mac, blob in devices_caps
            ]
        }
    )


async def _answer_the_sweep(broker: FakeBroker, list_payload: str) -> None:
    """Feed each sweep reply after its request was published, as the broker would."""
    async with asyncio.timeout(1.0):
        while (ADMIN_CONFIG_REQUEST, b"locations") not in broker.published:
            await asyncio.sleep(0)
        broker.deliver(LOCATIONS_TOPIC, devices())
        while (
            DEVICE_API_LIST_REQUEST,
            DEVICE_API_LIST_PAYLOAD,
        ) not in broker.published:
            await asyncio.sleep(0)
        broker.deliver(DEVICE_API_LIST_TOPIC, list_payload)


async def _sweep(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
    list_payload: str,
    *argv: str,
) -> int:
    """Run the tool over ``rows`` with the sweep answered by ``list_payload``."""
    broker = FakeBroker()
    broker.scripted_messages = _admin_discovery(*rows)
    a = _parse(monkeypatch, modules, "--timeout", "1", *argv, user=ADMIN_USER)
    answer = asyncio.create_task(_answer_the_sweep(broker, list_payload))
    try:
        return await modules.run(a, client_factory=broker.factory)
    finally:
        await answer


def test_modules_list_functions_needs_no_connection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The name list is a local read, so it runs without a host."""
    monkeypatch.setattr(sys, "argv", ["modules", "--list-functions"])
    assert modules.main() == 0
    assert "BUZZER               9" in capsys.readouterr().out


async def test_modules_refuses_a_restricted_account(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No connection is made: the tier is decided by the username alone."""
    broker = FakeBroker()
    a = _parse(monkeypatch, modules, user=USER)
    assert await modules.run(a, client_factory=broker.factory) == 2
    assert "is not the admin account" in capsys.readouterr().out
    assert broker.published == []


async def test_modules_prints_a_row_per_module(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _sweep(
        monkeypatch,
        [PANEL],
        _device_list((0xCB89, _caps((ModuleFunction.BUZZER, 1)))),
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "sweep: 1 answered, 0 silent" in printed
    assert "  16  M-DOT-4        42      -      -   1" in printed
    assert "1 of 1 modules" in printed
    # The installer's module names are private until asked for.
    assert "Sypialnia" not in printed


async def test_modules_prints_the_module_name_on_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _sweep(
        monkeypatch,
        [PANEL],
        _device_list((0xCB89, _caps((ModuleFunction.BUZZER, 1)))),
        "--show-names",
    )
    assert code == 0
    assert "Sypialnia" in capsys.readouterr().out


async def test_modules_filters_by_capability(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _sweep(
        monkeypatch,
        [PANEL, RELAY],
        _device_list(
            (0xCB89, _caps((ModuleFunction.BUZZER, 1))),
            (0xBEEF, _caps((ModuleFunction.IN_BIN, 8))),
        ),
        "--function",
        "buzzer",
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "M-DOT-4" in printed
    assert "M-IN-8s" not in printed
    assert "1 of 2 modules" in printed


async def test_modules_rejects_an_unknown_capability_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _sweep(
        monkeypatch, [PANEL], _device_list((0xCB89, _caps())), "--function", "nope"
    )
    assert code == 2
    assert "unknown function 'nope'; try --list-functions" in capsys.readouterr().out


async def test_modules_prints_one_capability_map(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An id the enum does not name still reads through under its number."""
    code = await _sweep(
        monkeypatch,
        [PANEL],
        _device_list((0xCB89, _caps((ModuleFunction.BUZZER, 1), (200, 3)))),
        "--module",
        "16",
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "row 16: M-DOT-4" in printed
    assert "  BUZZER               1" in printed
    assert "  unknown(200)         3" in printed


async def test_modules_reports_an_empty_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _sweep(
        monkeypatch, [PANEL], _device_list((0xCB89, _caps())), "--module", "99"
    )
    assert code == 1
    assert "no module on row 99" in capsys.readouterr().out


async def test_modules_watch_counts_the_modules_that_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh connection sees no reading: the server replays no such frame."""
    code = await _sweep(
        monkeypatch,
        [PANEL],
        _device_list((0xCB89, _caps())),
        "--watch",
        "0.01",
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "after  voltage  temperature  of 1" in printed
    assert "     0s        0            0" in printed
