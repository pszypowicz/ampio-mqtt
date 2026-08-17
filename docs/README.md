# ampio-mqtt protocol notes

Short, scannable notes for the reader who needs to reach further than
the source comments - "what shape comes back", "which id is
replacement-stable", "what else is reachable". Each file is one
screen.

| File                                             | Subject                                                                                                                                     |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- |
| [`protocol.md`](protocol.md)                     | Topic map: discovery requests, response topics, live state, raw channels, commands.                                                         |
| [`account-tiers.md`](account-tiers.md)           | What an administrator account gets that a standard one does not, with the measured latency difference.                                      |
| [`identity.md`](identity.md)                     | Which id is which - `mac` vs `mac_global`, `id` vs `funkcja` vs `leaf_id`, replacement-stable vs hardware-ordered.                          |
| [`classification.md`](classification.md)         | The `typ_komponentu` vocabulary, the `lin_wej` interpretation table, the visibility rule.                                                   |
| [`raw-channel-bridge.md`](raw-channel-bridge.md) | The `ampio/from/<MAC>/state/...` parallel topic surface; what is bridged today and what is not.                                             |
| [`discovery-flow.md`](discovery-flow.md)         | What `start()` does, what runs automatically vs on demand, and the connection liveness counters.                                            |
| [`untapped-surfaces.md`](untapped-surfaces.md)   | Reachable but unconsumed surfaces (resources, logs, MD5 hashes, CAN write tree, RPC bridge). Each entry links to its tracking issue.        |
| [`matter-bridge.md`](matter-bridge.md)           | The M-SERV's own Matter bridge, reverse-engineered: the `params` bit-4/bit-37 gate, its `type`/`leafId` classification table, and its gaps. |
