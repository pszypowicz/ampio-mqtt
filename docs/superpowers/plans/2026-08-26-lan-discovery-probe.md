# LAN discovery probe results (2026-08-26)

Live run of the Task 3 probe (`/tmp/mdns_probe.py`, zeroconf 0.149.16) against
the real LAN. Read-only: mDNS queries, one ping, one ARP read. No MQTT
publishes.

## A-record fact

`ampio.local` resolved over mDNS (`AddressResolverIPv4`, 3000 ms budget) to
`<m-serv-address>`.

Cross-checked against the broker host the library connects to: a plain
`socket.gethostbyname()` lookup of the broker host (from `admin.env`, loaded
line by line, never sourced) returned the same address, `<m-serv-address>`. The
mDNS name and the broker host are the same physical box.

## Service-type browse

`ZeroconfServiceTypes.find()` ran for 6 s and enumerated every service type
advertised anywhere on the LAN, then a `ServiceBrowser` per type ran for a
further 8 s (total browse window >= 14 s, comfortably over the 10 s bar). Ran
twice; both runs produced the identical set below.

24 service types found on the LAN. The full inventory is withheld here since
only the `_matter._tcp` entries relate to the M-SERV's address; the relevant
subset (two such sub-types observed, both redacted to the same placeholder
shape):

```
_I<fabric-id>._sub._matter._tcp.local.
_matter._tcp.local.
```

Of those 24, two distinct service instances resolve to the M-SERV's address
(`<m-serv-address>`), each showing up twice more via its own
`_sub._matter._tcp.local.` sub-type browse (6 raw hits, 2 unique instances):

```
_matter._tcp.local.  <fabric-id>-<node-id>._matter._tcp.local.  port=5540  txt={SII: 500, SAI: 300, SAT: 4000}
_matter._tcp.local.  <fabric-id>-<node-id>._matter._tcp.local.  port=5540  txt={SII: 500, SAI: 300, SAT: 4000}
```

(TXT keys/values shown decoded; on the wire they are bytes, e.g.
`b'SII': b'500'`.)

No other service type among the 24 resolves an instance to `<m-serv-address>`.
Neither the type strings nor the TXT keys reference Ampio, the M-SERV, or any
Ampio branding - `SII`/`SAI`/`SAT` are standard Matter operational discovery
parameters (session idle/active interval, session active threshold), not
identifying data.

## Verdict on `discovery.py`'s "no service type, no TXT" claim

Mixed - confirmed on identity, refuted on silence:

- **Confirmed**: nothing on the LAN advertises a service type or TXT record that
  identifies the M-SERV's address as Ampio. There is no `_ampio._tcp` or
  equivalent, and no TXT key/value carries Ampio-specific data. A discovery
  client cannot use service-type/TXT content to confirm "this is the Ampio
  broker" - the hostname A record remains the only Ampio-specific signal,
  matching the docstring's core point.
- **Refuted**: the address is not otherwise silent to a generic service-type
  browse. Two real `_matter._tcp` instances resolve to the M-SERV's IP and carry
  TXT records. These are almost certainly served by a Matter/Thread-related
  process co-located on the same physical host (see OUI below) rather than by
  the M-SERV/Ampio process itself, but a browse-then-filter-by-address discovery
  strategy would see them. Code that assumes an empty service-type result set at
  that address, or that treats "any TXT record present" as meaningful, would be
  wrong.

## MAC / OUI

After the mDNS contact, one ping and one ARP read against `<m-serv-address>`:

- OUI prefix: `b8:27:eb`
- Vendor lookup: Raspberry Pi Foundation (confirmed against a public OUI/vendor
  lookup).

No full MAC is recorded here. None of the six Matter mDNS hits above carry the
MAC in any field (TXT or otherwise) - the Matter instance names are fabric/node
identifiers, unrelated to the interface MAC.

## Hostname facts

- mDNS hostname: `ampio.local`, resolves as above.
- The ARP read returned no separate DNS/DHCP hostname for `<m-serv-address>`
  (unresolved, shown as `?` in the ARP entry) - no alternate spelling was
  observed. `ampio.local` is the only advertised hostname seen during this
  probe.

## Read-only confirmation

The probe issued mDNS queries only (address resolution, service-type
enumeration, per-type browse), one `ping -c 1`, and one `arp -n` read. No
`/api/set`, no MQTT publish of any kind.
