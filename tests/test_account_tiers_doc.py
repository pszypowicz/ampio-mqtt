"""The account-tiers page and the two client classes pin each other."""

from __future__ import annotations

import re
from pathlib import Path

from ampio_mqtt import AmpioAdminClient, AmpioClient

_PAGE = Path(__file__).resolve().parents[1] / "docs" / "account-tiers.md"
_MEMBER = re.compile(r"`([a-z_][a-z0-9_]*)(?:\([^)]*\))?`")


def _first_column_members(heading: str) -> set[str]:
    text = _PAGE.read_text()
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    section = text[start : end if end != -1 else len(text)]
    names: set[str] = set()
    for line in section.splitlines():
        if line.startswith("| `"):
            names.update(_MEMBER.findall(line.split("|")[1]))
    return names


def _public(cls: type) -> set[str]:
    return {name for name in dir(cls) if not name.startswith("_")}


def test_the_admin_table_pins_the_admin_only_members() -> None:
    documented = _first_column_members("## What AmpioAdminClient adds")
    assert documented == _public(AmpioAdminClient) - _public(AmpioClient)


def test_the_base_table_names_every_member_the_base_client_has() -> None:
    documented = _first_column_members("## What AmpioClient serves")
    assert documented == _public(AmpioClient)
