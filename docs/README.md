# ampio-mqtt protocol notes

Short, scannable notes that complement the source. The repo's
[`README.md`](../README.md) is the feature list and quick-start;
[`CHANGELOG.md`](../CHANGELOG.md) is the per-release history. These
notes are for the reader who needs to reach further: "what shape comes
back", "which id is replacement-stable", "what else is reachable that
the library does not consume yet".

Each file is one screen. If something is missing, the source is the
authoritative answer - the notes link to the relevant symbol rather
than restate it.

| File                                             | Subject                                                                                                                      |
| ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| [`protocol.md`](protocol.md)                     | Topic map: discovery requests, response topics, live state, raw channels.                                                    |
| [`identity.md`](identity.md)                     | Which id is which - `mac` vs `mac_global`, `id` vs `funkcja` vs `leaf_id`, replacement-stable vs hardware-ordered.           |
| [`classification.md`](classification.md)         | The `typ_komponentu` vocabulary, the `lin_wej` interpretation table, the visibility rule.                                    |
| [`raw-channel-bridge.md`](raw-channel-bridge.md) | The `ampio/from/<MAC>/state/...` parallel topic surface; what is bridged today and what is not.                              |
| [`discovery-flow.md`](discovery-flow.md)         | What `start()` does, what runs automatically vs on demand, and the connection liveness counters.                             |
| [`untapped-surfaces.md`](untapped-surfaces.md)   | Reachable but unconsumed surfaces (scenes, resources, logs, MD5 hashes, RPC bridge). Each entry links to its tracking issue. |
