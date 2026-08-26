# LAN discovery facts

What a Home Assistant config flow (or any other LAN-discovery consumer)
can actually rely on when looking for an Ampio M-SERV broker, drawn from
a live mDNS and DHCP/ARP probe of the M-SERV's LAN presence. Nothing
here is inferred from the protocol docs or the SDK source; every claim
below is either something the probe observed directly or is marked as
inference from an observation.

Authoritative source for the discovery mechanics themselves:
[`src/ampio_mqtt/discovery.py`](../src/ampio_mqtt/discovery.py) and
`AmpioClient.test_connection` /
[`AmpioServerInfo.key`](../src/ampio_mqtt/models.py).

## What the M-SERV publishes over mDNS

The M-SERV's hostname, `ampio.local`, resolves over mDNS to the
broker's address - the one Ampio-specific signal a browse-and-resolve
client gets. Nothing else on the LAN advertises a service type or TXT
record that identifies that address as Ampio: there is no `_ampio._tcp`
or equivalent, and no TXT key or value carries Ampio-specific data.

Observed: two separate `_matter._tcp` service instances, each with its
own `_sub._matter._tcp` sub-type advertisement, resolve to the same
address as `ampio.local`. Their TXT records carry only generic Matter
operational-discovery keys (session idle/active interval, session
active threshold), not anything Ampio-branded. Inference: this is most
likely a Matter/Thread-related process co-located on the same physical
host as the M-SERV, not something the M-SERV or Ampio process itself
serves - but a discovery client that browses every service type and
filters by address would see these two instances alongside the hostname
match, and should not treat their presence, or their TXT content, as
confirmation of an Ampio broker.

## Manifest zeroconf matching has nothing reliable to match

A Home Assistant integration manifest's `zeroconf` matcher keys on a
service type plus a TXT `properties` filter. Neither is available here:
no service type on the LAN is Ampio-specific, so there is nothing to
name in a `zeroconf` matcher's type field, and the one TXT content that
does resolve to the M-SERV's address (the co-located Matter instances'
SII/SAI/SAT keys) is generic Matter data, not an Ampio marker - matching
on it would just as readily fire for any other Matter/Thread device or
border router on the same LAN. A manifest `zeroconf` matcher is not a
usable discovery path for this integration.

## The DHCP matcher facts, and how weak they are

A DHCP matcher has two candidate signals from this probe, and both are
weak on their own:

- **OUI prefix `b8:27:eb`** (Raspberry Pi Foundation). This identifies
  the hardware vendor, not the M-SERV - it is the generic Raspberry Pi
  OUI block, shared by every Raspberry Pi on the same LAN, whatever
  they run. A DHCP matcher keyed on this OUI alone would match any Pi,
  not specifically an M-SERV.
- **Hostname.** The only hostname advertised anywhere on the LAN during
  the probe was `ampio.local` itself; the ARP read produced no separate
  DHCP-assigned hostname for the broker's address. So there is no
  alternate hostname spelling to add to a DHCP matcher - the hostname
  signal a matcher could use is the same `ampio.local` name the mDNS
  path already resolves directly, not an independent confirmation.

Neither signal is strong enough to identify an M-SERV on its own; both
are weaker than the mDNS hostname resolution already in `discover()`.

## No mDNS record carries the server mac

None of the records observed during the probe - the `ampio.local`
A-record, or either `_matter._tcp` instance - carry the M-SERV's mac in
any field. The Matter instance names are fabric/node identifiers
assigned by that co-located process, unrelated to the interface MAC,
and no ARP read exposes more than the OUI above.

So a config flow cannot derive
[`AmpioServerInfo.key`](../src/ampio_mqtt/models.py) (the mac-derived
unique id every config entry needs) from mDNS or DHCP alone. It has to
probe the broker itself: `AmpioClient.test_connection()` connects with
the candidate host, port, and credentials, requests the server info,
and returns an `AmpioServerInfo` whose `.key` property is the unique id
to store.

## `discover()` is the manual-flow fallback

`discover()` resolves `ampio.local` via an explicit mDNS A-record query
and confirms a listener on the broker port with a TCP probe, returning
a `DiscoveryResult` hint - not a confirmed identity. It is the manual
flow's convenience helper for finding a candidate host on the LAN
before credentials are known; `test_connection()` is still what
confirms the candidate is an Ampio M-SERV and produces the unique id.
