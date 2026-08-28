# ampio-mqtt protocol notes

These notes are for the reader who needs more than the source comments. They
answer "what shape comes back", "which id is replacement-stable", and "what else
is reachable". Each file is one screen.

Wire behavior documented here is verified against the baseline install (see the
README's supported versions). Claims that are still open are the marked case -
the text says in place exactly what is unverified.

| File                                             | Subject                                                                                                                     |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| [`protocol.md`](protocol.md)                     | Topic map: discovery requests, response topics, live state, raw channels, commands.                                         |
| [`events.md`](events.md)                         | The typed event stream: subscription patterns, what each event announces per tier, ordering, and the terminal signals.      |
| [`account-tiers.md`](account-tiers.md)           | What an administrator account gets that a standard one does not, with the measured latency difference.                      |
| [`identity.md`](identity.md)                     | Which id is which - `mac` vs `mac_global`, `id` vs `funkcja` vs `leaf_id`, replacement-stable vs hardware-ordered.          |
| [`classification.md`](classification.md)         | The classification model, the wire notes the code tables cannot carry, the consumer-CI key contract.                        |
| [`raw-channel-bridge.md`](raw-channel-bridge.md) | The `ampio/from/<MAC>/state/...` parallel topic surface. What is bridged today and what is not.                             |
| [`discovery-flow.md`](discovery-flow.md)         | What `connect()` does, what runs automatically vs on demand, LAN discovery, and the connection liveness counters.           |
| [`lan-discovery.md`](lan-discovery.md)           | What the M-SERV publishes over mDNS and DHCP, and why the config flow's unique id comes from a probe of the broker instead. |
| [`untapped-surfaces.md`](untapped-surfaces.md)   | Reachable but unconsumed surfaces, each with a self-contained description and a link to its work-tracking notes.            |
