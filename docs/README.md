# ampio-mqtt protocol notes

These notes are for the reader who needs more than the source comments. They
answer "what shape comes back", "which id is replacement-stable", and "what else
is reachable". Each file covers one subject.

Wire behavior documented here is verified against the baseline install (see the
README's supported versions). Claims that are still open are the marked case -
the text says in place exactly what is unverified.

| File                                               | Subject                                                                                                                     |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| [`protocol.md`](protocol.md)                       | Topic map: the two trees, discovery requests, response topics, the description-record pair, and live state.                 |
| [`commands.md`](commands.md)                       | The `/api` verb vocabulary, the client method behind each verb, the state-echo confirmation, and the cover and scene notes. |
| [`panel-writes.md`](panel-writes.md)               | The raw CAN output frame for panel status LEDs, relays, and open-collector outputs, and the panel buzzer.                   |
| [`designer-surfaces.md`](designer-surfaces.md)     | The legacy CAN bridge endpoints and every surface the Designer itself uses, the flag write frames included.                 |
| [`bus-events.md`](bus-events.md)                   | Bus events: how to raise one, how to receive one, and which tier gets which.                                                |
| [`events.md`](events.md)                           | The typed event stream: subscription patterns, what each event announces per tier, ordering, and the terminal signals.      |
| [`account-tiers.md`](account-tiers.md)             | What an administrator account gets that a standard one does not, with the measured latency difference.                      |
| [`identity.md`](identity.md)                       | Which id is which - `mac` vs `mac_global`, `id` vs `funkcja` vs `leaf_id`, replacement-stable vs hardware-ordered.          |
| [`visibility.md`](visibility.md)                   | The `visible` predicate, the `params` bit enum, the read-only and bell markers, and deletion on the wire.                   |
| [`description-records.md`](description-records.md) | The description record in each module: the Matter tag, the location pointer, the list reply, the join rule, and the sweep.  |
| [`classification.md`](classification.md)           | The classification model, the wire notes the code tables cannot carry, the consumer-CI key contract.                        |
| [`raw-channel-bridge.md`](raw-channel-bridge.md)   | The `ampio/from/<MAC>/state/...` parallel topic surface. What is bridged today and what is not.                             |
| [`discovery-flow.md`](discovery-flow.md)           | What `connect()` does, what runs automatically vs on demand, LAN discovery, and the connection liveness counters.           |
| [`lan-discovery.md`](lan-discovery.md)             | What the M-SERV publishes over mDNS and DHCP, and why the config flow's unique id comes from a probe of the broker instead. |
| [`untapped-surfaces.md`](untapped-surfaces.md)     | Reachable but unconsumed surfaces, each with a self-contained description and a link to its work-tracking notes.            |
