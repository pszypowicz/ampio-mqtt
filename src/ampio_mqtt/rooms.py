"""Pure parsers for the M-SERV's `data/groups` + `data/group_devices` payloads.

The Ampio M-SERV's MQTT surface exposes two endpoints that together let a
client reconstruct "which Ampio object lives in which room":

- ``ampio/fromDB/<user>/data/groups`` -> ``{"List": [{"id", "id_rodzica",
  "opis_menu", ...}, ...]}`` - the list of rooms (and hierarchy parents).
- ``ampio/fromDB/<user>/data/group_devices`` -> ``{"List": [{"id_grupy",
  "id_obiektu", ...}, ...]}`` - the object-to-room join table.

Both are requested by publishing the keyword ("groups" / "group_devices") to
``ampio/control/<user>/data``. The MQTT roundtrip lives on ``AmpioClient``;
this module only contains the pure JSON-to-dict join, kept here so it stays
trivially unit-testable without touching MQTT.

The integration consumer (a Home Assistant integration backed by this
library) is expected to pass the joined dict's values as
``DeviceInfo.suggested_area`` once at device creation, matching the pattern
used by ``lutron_caseta`` and ``niko_home_control``. See the HA dev blog at
https://developers.home-assistant.io/blog/2025/08/01/suggested-area-removed-from-deviceentry/ -
input-side ``suggested_area`` is still officially supported through HA Core
2026.9 and beyond; only the read-side ``DeviceEntry.suggested_area`` is being
removed.
"""

from __future__ import annotations

from typing import Any


def join_rooms(
    groups_data: dict[str, Any], group_devices_data: dict[str, Any]
) -> dict[int, str]:
    """Join the two M-SERV payloads into ``{ampio_object_id: room_name}``.

    Tolerates missing / mistyped entries (returns them as no-ops). Objects
    that appear in multiple groups map to the first room encountered, since
    Home Assistant allows one area per device and the join table has no
    "primary group" marker.
    """
    group_names: dict[int, str] = {}
    for g in groups_data.get("List", []):
        gid = g.get("id")
        name = g.get("opis_menu")
        if isinstance(gid, int) and isinstance(name, str) and name:
            group_names[gid] = name

    room_map: dict[int, str] = {}
    for gd in group_devices_data.get("List", []):
        oid = gd.get("id_obiektu")
        gid = gd.get("id_grupy")
        if not isinstance(oid, int) or not isinstance(gid, int):
            continue
        if oid in room_map:
            continue  # first match wins; HA allows one area per device
        name = group_names.get(gid)
        if name:
            room_map[oid] = name
    return room_map
