# Presence detection and simulation

The M-SERV creates two rows of its own, `detekcja` for presence detection and
`symulacja` for presence simulation. Designer lists both types but cannot
create, delete or configure them. The Ampio app is the configuration surface.
Neither row is a module output. Neither carries a leaf, and neither answers a
verb. The library therefore does not model them as objects. They are two
attributes of the client, with their own types and their own event.

## The two attributes

`AmpioClient.presence_detection` is a `PresenceDetection` or None.
`AmpioClient.presence_simulation` is a `PresenceSimulation` or None. Both
account tiers receive both rows. Each attribute reads None until the catalogue
lists its row, and it reads None while the row carries the hidden bit. On a
standard account the hidden bit and `active` follow the params table, which
arrives during initial discovery.

| Type                 | Field         | Source                              |
| -------------------- | ------------- | ----------------------------------- |
| `PresenceDetection`  | `id`          | the catalogue row                   |
| `PresenceDetection`  | `name`        | `opis_menu`                         |
| `PresenceDetection`  | `home_status` | the row's per-object state, or None |
| `PresenceSimulation` | `id`          | the catalogue row                   |
| `PresenceSimulation` | `name`        | `opis_menu`                         |
| `PresenceSimulation` | `active`      | the row's `czas` column, 1 for on   |

Neither type has `is_on` or `state`.

## The detection code

The detection row's state is a home-status code, not 255 or 0. The M-SERV
computes it from the sensors the app links to the row. Code 5 is "home empty" in
the Ampio app. The other codes are unknown. A code that is not an integer is a
server fault, and the library refuses the push.

The `on` stamp on that push is not the change time. The library does not expose
it.

The states snapshot seeds `home_status`. After a reconnect the snapshot corrects
a code that no push replaced since the reconnect. A live push always replaces
the code. A hidden row keeps its code and shows it when the row returns. A row
the catalogue stops listing loses its code.

## The simulation switch

The app switches the simulation on and off. The switch is the `czas` column of
the simulation row, 1 for on. After each switch the M-SERV pushes the params
table into every account namespace, and `active` follows it.

The simulation row carries no state.

## The event

`PresenceChanged` carries both rows as they read after a change, and None for a
row the catalogue does not list or hides. It fires when a row appears or leaves,
when a name or the switch changes, and when the detection code moves. A repeated
catalogue reply that changes nothing fires nothing. Subscribe with
`client.subscribe(listener, of=PresenceChanged)`. The `object_id` filter does
not apply, because the event carries no object.

## Writes

The library takes no write to either row. The app configures both over surfaces
the library does not consume. See [`visibility.md`](visibility.md) for the wire
form of the linked devices and the sensor roles.
