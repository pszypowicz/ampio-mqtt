# Bus events

This page continues [`protocol.md`](protocol.md).

## Bus events

Events are logical signals numbered 1-65535 that Ampio's own logic raises and
reacts to. A wall-panel press can raise one, and a scenario can be bound to one.
They are independent of objects - an event carries no state, and it drives
whatever logic the installer bound to it.

| Direction | Topic / payload                                  |
| --------- | ------------------------------------------------ |
| Raise     | `ampio/control/<user>/api` ← `/api/setEvent/<n>` |
| Receive   | `ampio/from/<MAC>/event` → `<n>`                 |

The MAC on a received event identifies what raised it. A panel press carries the
module's own address. An event injected through the command surface carries the
M-SERV's (`1` by default).

The two directions are gated differently:

- **Raising** works on both account tiers, and nothing bounds it. The Ampio app
  shows a per-user rights list per event, but that list is not enforced here. A
  standard account raised an event it had no right to, checked against a control
  event created without that right. Because the logic behind an event can drive
  anything, this is how an account reaches objects it cannot command directly.
- **Receiving** is administrator-only. It rides the raw tree. A standard account
  that holds the event's right still sees nothing - not on the raw tree, and not
  anywhere in its own namespace.

On the CAN side an event is frame type `0x2B` with a 16-bit little-endian
number, low byte first. The frame is `FE 2B BD 00` for 189 and `FE 2B BD BD`
for 48573. A legacy 8-bit event is one whose high byte is zero.

That layout invites a suspicion worth a rule-out. Does logic bound to an 8-bit
event also fire for a 16-bit event that shares one of its bytes? This was tested
against a module rule bound to event 189 (`0x00BD`). Neither `0xBDBD` nor
`0xBD00` moved it, 189 itself toggled reliably, and an unrelated event did
nothing. The match is on the full 16-bit value, at least on the M-DOT firmware
this ran against.

**The M-SERV raises event 254 from its own MAC whenever a client asks for a
discovery refresh**, so `connect()` normally produces one. It is not periodic. A
purely passive listener sees no events at all. A consumer that only cares about
panel presses must filter on the originating MAC, and must not treat every event
as user intent.
