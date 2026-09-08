# LAN discovery facts

This page lists what a Home Assistant config flow, or any other LAN-discovery
consumer, can rely on in a search for an Ampio M-SERV broker. Each inference is
marked as one.

Authoritative source for the discovery mechanics themselves:
[`src/ampio_mqtt/discovery.py`](../src/ampio_mqtt/discovery.py) and
`AmpioClient.check_connection` /
[`AmpioServerInfo.server_key`](../src/ampio_mqtt/models.py).

## What the M-SERV publishes over mDNS

The M-SERV's hostname, `ampio.local`, resolves over mDNS to the broker's
address. That hostname is the one Ampio-specific signal a browse-and-resolve
client gets. Nothing else on the LAN advertises a service type or TXT record
that identifies that address as Ampio. There is no `_ampio._tcp` or equivalent,
and no TXT key or value carries Ampio-specific data.

Two separate `_matter._tcp` service instances, each with its own
`_sub._matter._tcp` sub-type advertisement, resolve to the same address as
`ampio.local`. Their TXT records carry only generic Matter operational-discovery
keys (session idle/active interval, session active threshold), and nothing
Ampio-branded. Inference: this is most likely a Matter or Thread process
co-located on the same physical host, not something the M-SERV or Ampio process
itself serves. But a discovery client that browses every service type and
filters by address will see these two instances next to the hostname match. It
must not treat their presence, or their TXT content, as confirmation of an Ampio
broker.

## Manifest zeroconf matching has nothing reliable to match

A Home Assistant integration manifest's `zeroconf` matcher keys on a service
type plus a TXT `properties` filter. Neither is available here. No service type
on the LAN is Ampio-specific, so there is nothing to name in the matcher's type
field. The one TXT content that resolves to the M-SERV's address is the
SII/SAI/SAT keys of the co-located Matter instances. That is generic Matter
data, not an Ampio marker. A match on it fires equally for any other Matter or
Thread device or border router on the same LAN. A manifest `zeroconf` matcher is
not a usable discovery path for this integration.

## The DHCP matcher facts, and how weak they are

A DHCP matcher has two candidate signals, and both are weak on their own:

- **OUI prefix `b8:27:eb`** (Raspberry Pi Foundation). This identifies the
  hardware vendor, not the M-SERV. It is the generic Raspberry Pi OUI block,
  shared by every Raspberry Pi on the same LAN. A DHCP matcher keyed on this OUI
  alone matches any Pi, not specifically an M-SERV.
- **Hostname.** The DHCP client hostname is unverified. ARP maps an IP to a MAC
  only, and no lease table read exists. A matcher keyed on the hostname needs
  that read first.

Neither signal is strong enough to identify an M-SERV on its own. Both are
weaker than the mDNS hostname resolution already in `discover()`.

## No mDNS record carries the server mac

None of the records - the `ampio.local` A-record, or either `_matter._tcp`
instance - carries the M-SERV's mac in any field. The Matter instance names are
fabric and node identifiers from that co-located process, unrelated to the
interface MAC. No ARP read exposes more than the OUI above. The server mac
behind `server_key` is the M-SERV's CAN bus mac, not its Ethernet address.

So a config flow cannot derive
[`AmpioServerInfo.server_key`](../src/ampio_mqtt/models.py) (the mac-derived
unique id every config entry needs) from mDNS or DHCP alone. It must probe the
broker itself. `AmpioClient.check_connection()` connects with the candidate
host, port, and credentials, requests the server info, and returns an
`AmpioServerInfo`. Its `.server_key` property is the unique id to store.

## `discover()` is the manual-flow fallback

`discover()` returns a `DiscoveryResult` (`host`, `port`, `address`), a hint and
not a confirmed identity. Its mechanics are in
[`discovery-flow.md`](discovery-flow.md). The manual flow uses it to find a
candidate host on the LAN before credentials are known. `check_connection()`
still confirms that the candidate is an Ampio M-SERV, and it produces the unique
id.
