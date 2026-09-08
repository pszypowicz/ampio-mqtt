"""Tests for the probe scripts in tools/.

Each script's ``run()`` takes a client factory, so the shared FakeBroker
drives it end to end without a broker. The scripts import as top-level
modules through the ``pythonpath`` entry in the pytest configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from types import ModuleType

import aiomqtt
import dump
import pytest
import set_object
import smoke_test
from conftest import (
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
)

from ampio_mqtt import AmpioClient

API_TOPIC = f"ampio/control/{USER}/api"


def _parse(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, *argv: str
) -> argparse.Namespace:
    """The script's own ``parse_args`` over ``argv``, credentials filled."""
    monkeypatch.setattr(
        sys,
        "argv",
        [module.__name__, "--host", "h", "--username", USER, "--password", "p", *argv],
    )
    return module.parse_args()


def _discovery(*rows: dict) -> list[Message]:
    """The restricted tier's four initial replies, so ``connect()`` completes."""
    return [
        Message(DATA_DEVICES_TOPIC, details(*rows).encode()),
        Message(PARAMS_DEVICES_TOPIC, devices().encode()),
        Message(STATES_TOPIC, devices().encode()),
        Message(INFO_TOPIC, info(mac=1, userId="7").encode()),
    ]


# --- dump.py ----------------------------------------------------------------


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
        Message(f"ampio/fromDB/{USER}/ob/64/state", b'{"state":"0"}'),
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
        Message(f"ampio/fromDB/{USER}/ob/41/state", b'{"state":"22.5"}'),
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
