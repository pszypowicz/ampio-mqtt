"""Shared fixture payloads and the in-process fake M-SERV responder.

Each claim driver runs the responder as a task in its own process, so the
only external moving part is the local mosquitto on port 18831.

Tier follows the username exactly as the real M-SERV decides it: the
responder serves the ``config`` catalogues only to the reserved ``admin``
login and the app-sync ``data`` pair to any other account, and the info
reply's ``userId`` is -1 for admin and a users-table row id otherwise.
Pick the tier per script by connecting the client (and the responder) as
``USER`` or ``ADMIN``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid

import aiomqtt

PORT = 18831
USER = "u"  # an app-created account: the restricted tier
ADMIN = "admin"  # the reserved administrator login


def ob_state_topic(user: str, oid: int = 5) -> str:
    return f"ampio/fromDB/{user}/ob/{oid}/state"


def devices_resp_topic(user: str) -> str:
    return f"ampio/fromDB/{user}/config/devices"


STATE_TOPIC_OB5 = ob_state_topic(USER)


def details(*items: dict) -> str:
    return json.dumps({"Status": 0, "List": list(items)})


def rows(*items: dict) -> str:
    return json.dumps({"List": list(items)})


def info(**fields: object) -> str:
    return json.dumps({"Results": fields})


# Object 5: a flag on module 2, channel 3. The leafId embeds the module's
# effective bus mac (0xA = MOD2["mac"]) in the wire's five-segment shape,
# so on the admin tier the raw topic ampio/from/A/state/f/3 routes to it.
OB5 = {
    "id": 5,
    "id_urzadzenia": 2,
    "typ_komponentu": "flaga",
    "opis_menu": "Flag 5",
    "interpretacja": 0,
    "funkcja": 3,
    "leafId": "0_a_76_0_0",
    "params": 0,
    "stan_json": json.dumps({"state": "0", "on": 1000}),
}

MOD2 = {
    "id": 2,
    "mac": 10,
    "mac_global": 555,
    "nazwa_urzadzenia": "old",
    "typ_urzadzenia": 1,
    "wersja_softu": 3,
    "wersja_pcb": 1,
}

# Object 64: a relay on module 16 (mac 0xCB89/52105), the id and leafId the
# real house's join-proof relay carries (#25) - a separate object and module
# from OB5/MOD2 so the device_api join fixtures below do not disturb the
# raw-topic and module_for claims that key off OB5's shape.
OB64 = {
    "id": 64,
    "id_urzadzenia": 16,
    "typ_komponentu": "przekaznik",
    "opis_menu": "Relay 64",
    "interpretacja": 0,
    "funkcja": 0,
    "leafId": "0_cb89_257_2_0",
    "params": 0,
    "stan_json": json.dumps({"state": "0"}),
}

MOD_CB89 = {
    "id": 16,
    "mac": 0xCB89,
    "mac_global": 0xCB89,
    "nazwa_urzadzenia": "relay-module",
    "typ_urzadzenia": 1,
    "wersja_softu": 3,
    "wersja_pcb": 1,
}

# The Designer "Lokalizacja" name table entry the join in resolve_locations()
# resolves object 64's out_loc pointer through.
LOCATIONS = ({"id": 14, "opis_menu": "Potter"},)


def frame(
    desc_type: int, out_no: int, out_loc: int, out_type: int, desc: str
) -> bytes:
    """One CAN-resident description entry, same layout as test_descriptions's frame()."""
    body = desc.encode()
    length = 10 + len(body)
    return (
        b"".join(
            v.to_bytes(2, "little")
            for v in (length, desc_type, out_no, out_loc, out_type)
        )
        + body
    )


# Object 64's description entry: OUTPUTS class (12), out_no 0 matches its
# leafId's last segment, out_loc 14 joins the Potter row above, out_type 256
# is the Matter On/Off Light tag resolve_locations() refines onto the object.
DESCRIPTIONS_REPLY = json.dumps(
    {"descriptions": base64.b64encode(frame(12, 0, 14, 256, "L")).decode()}
)


def response_table(
    user: str, *, server_version: str = "1865"
) -> dict[tuple[str, str], tuple[str, str]]:
    """(request topic, payload) -> (reply topic, reply payload)."""
    admin = user == ADMIN
    uid = -1 if admin else 7
    table = {
        (f"ampio/control/{user}/info", ""): (
            f"ampio/fromDB/{user}/data/info",
            info(userId=uid, mac=555, serverVersion=server_version),
        ),
        (f"ampio/control/{user}/states", ""): (
            f"ampio/fromDB/{user}/data/states",
            rows({"id": 5, "stan_json": json.dumps({"state": "0", "on": 1000})}),
        ),
    }
    if admin:
        table[(f"ampio/control/{user}/config", "devicesDetails")] = (
            f"ampio/fromDB/{user}/config/devicesDetails",
            details(OB5, OB64),
        )
        table[(f"ampio/control/{user}/config", "devices")] = (
            devices_resp_topic(user),
            rows(MOD2, MOD_CB89),
        )
        table[(f"ampio/control/{user}/config", "locations")] = (
            f"ampio/fromDB/{user}/config/locations",
            rows(*LOCATIONS),
        )
    else:
        ob5_app = {k: v for k, v in OB5.items() if k not in ("params", "stan_json")}
        table[(f"ampio/control/{user}/data", "devices")] = (
            f"ampio/fromDB/{user}/data/devices",
            details(ob5_app),
        )
        table[(f"ampio/control/{user}/data", "params_devices")] = (
            f"ampio/fromDB/{user}/data/params_devices",
            rows({"id": 5, "params": 0}),
        )
    return table


async def fake_mserv(
    *,
    user: str = USER,
    server_version: str = "1865",
    ready: asyncio.Event | None = None,
    overrides: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> None:
    """Answer discovery requests like an M-SERV until cancelled."""
    table = response_table(user, server_version=server_version)
    if overrides:
        table.update(overrides)
    async with aiomqtt.Client(
        hostname="127.0.0.1",
        port=PORT,
        identifier=f"fake-mserv-{uuid.uuid4().hex[:8]}",
    ) as client:
        await client.subscribe("ampio/control/#", qos=1)
        await client.subscribe("device_api/to/#", qos=1)
        if ready is not None:
            ready.set()
        async for msg in client.messages:
            topic = str(msg.topic)
            if topic.startswith("device_api/to/") and topic.endswith("/get_data"):
                mac_hex = topic.removeprefix("device_api/to/").removesuffix(
                    "/get_data"
                )
                await client.publish(
                    f"device_api/from/{mac_hex.upper()}/info",
                    DESCRIPTIONS_REPLY.encode(),
                    qos=1,
                )
                continue
            hit = table.get((topic, msg.payload.decode()))
            if hit is not None:
                await client.publish(hit[0], hit[1].encode(), qos=1)


async def inject(topic: str, payload: str) -> None:
    """Publish one message from a throwaway session."""
    async with aiomqtt.Client(
        hostname="127.0.0.1",
        port=PORT,
        identifier=f"injector-{uuid.uuid4().hex[:8]}",
    ) as client:
        await client.publish(topic, payload.encode(), qos=1)
