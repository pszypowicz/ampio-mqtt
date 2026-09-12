#!/usr/bin/env python3
"""Print the module inventory an admin account receives.

Read only. Connects, runs one description sweep to fill the capability
map and the record bundles, prints a row per module, and disconnects.
Sends no command to any module.

Admin account only: the description sweep reads the `device_api` tree,
which answers no other account.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable

import aiomqtt

from ampio_mqtt import AccessTier, AmpioClient, ModuleFunction


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the command line."""
    p = argparse.ArgumentParser(
        description=(
            "Print the module inventory: model, capabilities, firmware, and "
            "the readings a module broadcasts about itself. Read only."
        ),
        epilog=(
            "Examples:\n"
            "  uv run python tools/modules.py\n"
            "  uv run python tools/modules.py --function BUZZER\n"
            "  uv run python tools/modules.py --module 12 --show-names\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--host",
        default=os.environ.get("AMPIO_HOST"),
        help="M-SERV host (default: AMPIO_HOST)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AMPIO_PORT", "1883")),
        help="MQTT port (default: AMPIO_PORT, else 1883)",
    )
    p.add_argument(
        "--username",
        default=os.environ.get("AMPIO_USERNAME"),
        help="account name (default: AMPIO_USERNAME)",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("AMPIO_PASSWORD"),
        help="account password (default: AMPIO_PASSWORD)",
    )
    p.add_argument(
        "--function",
        metavar="NAME",
        help=(
            "list only modules whose capability map carries this function, "
            "for example BUZZER. Use --list-functions for the names."
        ),
    )
    p.add_argument(
        "--module",
        type=int,
        metavar="ROW",
        help="print one module's full capability map, by its Designer row id",
    )
    p.add_argument(
        "--show-names",
        action="store_true",
        help="include the installer's module names, which are private",
    )
    p.add_argument(
        "--list-functions",
        action="store_true",
        help="print every capability name the library knows, then exit",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="seconds to wait for the description sweep (default: 20)",
    )
    p.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help=(
            "hold the connection this long and report how many modules have "
            "broadcast a voltage or a temperature as time passes. A fresh "
            "connection starts with almost none, because the client drops the "
            "retained frames that arrive before the module catalogue."
        ),
    )
    return p.parse_args(argv)


async def watch_readings(client: AmpioClient, seconds: float) -> None:
    """Count the modules reporting a reading, as the frames arrive."""
    marks = [t for t in (5.0, 15.0, 30.0, 60.0, 120.0) if t < seconds]
    marks.append(seconds)
    total = len(client.modules)
    print(f"\n{'after':>7}  {'voltage':>7}  {'temperature':>11}  of {total}")
    waited = 0.0
    for mark in marks:
        await asyncio.sleep(mark - waited)
        waited = mark
        volts = sum(1 for m in client.modules.values() if m.supply_voltage is not None)
        temps = sum(1 for m in client.modules.values() if m.temperature is not None)
        print(f"{mark:>6.0f}s  {volts:>7}  {temps:>11}")


async def run(
    a: argparse.Namespace,
    client_factory: Callable[[], aiomqtt.Client] | None = None,
) -> int:
    """Connect, sweep, print, disconnect.

    ``client_factory`` is the test seam for the session.
    """
    client = AmpioClient(
        a.host,
        a.username,
        a.password,
        port=a.port,
        mqtt_client_factory=client_factory,
    )
    if client.access_tier is not AccessTier.ADMIN:
        print(
            f"{a.username!r} is not the admin account. "
            "The device_api tree answers no other account."
        )
        return 2
    await client.connect()
    try:
        sweep = await client.resolve_records(timeout=a.timeout)
        print(
            f"sweep: {len(sweep.answered_macs)} answered, "
            f"{len(sweep.silent_macs)} silent"
        )
        modules = dict(sorted(client.modules.items()))

        if a.module is not None:
            module = modules.get(a.module)
            if module is None:
                print(f"no module on row {a.module}")
                return 1
            print(f"row {a.module}: {module.model or '?'}")
            for fn, count in sorted(module.capabilities.items()):
                try:
                    name = ModuleFunction(fn).name
                except ValueError:
                    name = f"unknown({fn})"
                print(f"  {name:<20} {count}")
            return 0

        wanted: int | None = None
        if a.function:
            try:
                wanted = int(ModuleFunction[a.function.upper()])
            except KeyError:
                print(f"unknown function {a.function!r}; try --list-functions")
                return 2

        header = f"{'row':>4}  {'model':<12} {'sw':>4} {'volt':>6} {'temp':>6}  caps"
        if a.show_names:
            header += "  name"
        print(header)
        shown = 0
        for row, m in modules.items():
            if wanted is not None and wanted not in m.capabilities:
                continue
            shown += 1
            volt = f"{m.supply_voltage:.1f}" if m.supply_voltage is not None else "-"
            temp = f"{m.temperature:.0f}" if m.temperature is not None else "-"
            line = (
                f"{row:>4}  {(m.model or '?'):<12} {m.wersja_softu or '-'!s:>4} "
                f"{volt:>6} {temp:>6}  {len(m.capabilities):>2}"
            )
            if a.show_names:
                line += f"  {m.nazwa_urzadzenia or '-'}"
            print(line)
        print(f"{shown} of {len(modules)} modules")
        if a.watch:
            await watch_readings(client, a.watch)
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    """Entry point."""
    a = parse_args()
    if a.list_functions:
        for fn in ModuleFunction:
            print(f"{fn.name:<20} {int(fn)}")
        return 0
    if not a.host:
        print("missing --host (or AMPIO_HOST env)", file=sys.stderr)
        return 2
    if not a.username:
        print("missing --username (or AMPIO_USERNAME env)", file=sys.stderr)
        return 2
    return asyncio.run(run(a))


if __name__ == "__main__":
    raise SystemExit(main())
