# mtvpn

Selective-VPN domain routing on MikroTik RouterOS 7. Domains for the services you
pick go through your VPN gateway, everything else goes direct.

Two parts, with a strict split of ownership:

- `fresh-router.rsc` — one-shot `/import` template for a factory-fresh router. Owns
  *all* router config: the address-list, the `mtvpn:*` mangle rules, the routing
  table, the DoH forwarder.
- `mtvpn.py` — python3 CLI (stdlib only) that fetches domain lists and fills that
  address-list over SSH. It owns only what is *in* the list.

## DNS

Two paths:

- **Everything else** resolves via the ISP's plain UDP/53 servers, straight out the
  WAN — supplied by DHCP (`use-peer-dns=yes`, no `servers=` set by hand), or set by
  hand on a [static WAN](#static-wan-address-no-isp-dhcp).
- **Tunneled services** resolve via `https://dns.google/dns-query`. `fresh-router.rsc`
  creates a `/ip dns forwarders` entry named `vpn-doh` and pins `dns.google` to
  8.8.8.8 with a static A record and into `to_vpn_list` by hostname, so the DoH
  session itself rides the tunnel. mtvpn puts `forward-to=vpn-doh` on every
  per-domain FWD entry it installs.

So tunneled names resolve from the exit node's vantage point (the addresses that
land in `to_vpn_list` are the ones nearest the path they will be fetched over),
direct names resolve locally, and a dead tunnel costs you only the tunneled
services — general DNS keeps working.

> LAN clients must use **the router as their only DNS server**, or subdomain
> coverage silently degrades. With tailscale that means `--accept-dns=false` and
> the router in the host's `/etc/resolv.conf`.

## Domain sources

Every service names its source explicitly; nothing is inferred.

| entry | source |
|---|---|
| `iplist:youtube.com` | [iplist.opencck.org](https://github.com/rekryt/iplist) — a **site** |
| `iplist:apple` | iplist — a **group** (every site in it) |
| `iplist:beta:cloudflare.com` | iplist with the portal pinned (`main`/`beta`/`russia`) |
| `v2fly:anthropic` | [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community) |
| `anthropic` | same as `v2fly:anthropic` — a bare name is the v2fly alias |
| `https://…` | a raw URL in either format — including your own list of domains |
| `mine=https://…` | the same, with the router tag named explicitly |

Any URL serving one domain per line works: that format is a subset of v2fly's.
`full:`, `domain:` and `include:` are honoured; `regexp:`/`keyword:` are reported as
skipped. `#` starts a comment anywhere in the line. The tag comes from the URL's
last path segment minus its extension, or from `<tag>=` if you give one.

iplist spreads its catalog over three near-disjoint portals, so `iplist:<selector>`
tries each in turn and takes the first that has it. `search` shows which:

```sh
./mtvpn.py search apple -s iplist

iplist:apple              beta    group  10 site(s)
iplist:apple.com          beta    site   (apple)
```

The router tag is the selector **without** its prefix, so `v2fly:youtube` still owns
the `comment=youtube` entries a bare `youtube` installed. Switching a service to a
differently-named selector changes the tag, so clear the old one out:

```sh
./mtvpn.py remove chesscom
./mtvpn.py add iplist:chess.com
```

## Hosted service lists

The set of services can live on a server instead of in every router's config: one
selector per line — exactly what `services:` holds, so a `services:` block pastes in
as-is (leading `- ` is stripped). `#` comments work at line start or after
whitespace, which leaves raw-URL entries intact.

```yaml
service_lists:
  - https://files.example.com/mtvpn-tunneled.txt
services:
  - v2fly:anthropic     # this router only, on top of the list
```

`-l/--from-list` takes a URL or a path and is repeatable. `add`/`remove` record and
forget the URL in `service_lists:`; `update` never edits the config, so a one-off
`-l` stays one-off.

`--urls-only` narrows a refresh to services whose source is a raw URL. `--prune`
removes every service tag on the router the effective set no longer names; it applies
only to a full `update` and leaves the router's own `telegram-cidr` entries alone.

## Config

`mtvpn.yaml`, or one file per router selected with `-c`:

```yaml
# full ssh command for reaching the router; anything ssh accepts
ssh: ssh -J jumphost 10.230.1.1
# optional scp override for the /import fast path; {local}/{remote} substituted
# scp: scp -J jumphost {local} 10.230.1.1:{remote}
# the address-list the router routes through the tunnel
list: to_vpn_list
# the /ip dns forwarders entry mtvpn points its FWD entries at
doh_forwarder: vpn-doh
service_lists:
  - https://files.example.com/mtvpn-tunneled.txt
services:
  - v2fly:anthropic
  - iplist:claude.ai
```

`list:` and `doh_forwarder:` are the only router-side names mtvpn needs, and both
must match `fresh-router.rsc`'s `$vpnList` / `$dohForwarder`. A `list:` no rule
matches on fails silently: entries are written, nothing is routed, nothing errors.

## Usage

```sh
./mtvpn.py add v2fly:anthropic iplist:chatgpt.com
./mtvpn.py add -l https://files.example.com/mtvpn-tunneled.txt

./mtvpn.py update                  # re-fetch upstream lists, refresh everything
./mtvpn.py update --urls-only      # refresh only your own raw-URL domain lists
./mtvpn.py update --prune          # ...and drop services the lists no longer name
./mtvpn.py remove netflix
./mtvpn.py list -v                 # what's installed on the router, by service

./mtvpn.py -c mtvpn-hex.yaml add v2fly:openai   # another router

# no router needed
./mtvpn.py -n add v2fly:anthropic  # dry-run: print the RouterOS commands
./mtvpn.py search google
./mtvpn.py domains openai
```

`domains` and `search` never touch the router; everything else takes `-r "ssh <...>"`
to override the config's `ssh:`.

`add`/`update` are idempotent: entries tagged with the service comment are replaced
wholesale, and pre-existing *untagged* entries for the same domains are adopted rather
than duplicated. Entries commented `mtvpn:*` are infrastructure pins and are never
adopted, removed or pruned.

## Setting up a new router

Open `fresh-router.rsc`, edit the PARAMETERS block, complete the PREREQUISITES it
lists, and `/import` it. Notes:

- **IPv6 is disabled** (takes effect on reboot). The selective-routing path is
  IPv4-only, so a dual-stack client would otherwise reach an AAAA-capable service
  direct over the WAN.
- Firewall and mangle rules match on the interface lists `LANiface`/`WANiface`.
- There are no `dstnat` rules, so the template carries no bogon-source drop. **Add
  `not_in_internet` back if you ever configure a port forward.**
- To use mtvpn against a router set up some other way, give it the equivalent of the
  template's `mtvpn:*` rules and the `vpn-doh` forwarder.

### Static WAN address (no ISP DHCP)

Substitute for the `/ip dhcp-client` line in the template, and add `servers=` to
`/ip dns set` — nothing fills `dynamic-servers` without the DHCP client. Plain
UDP/53, no DoH: the DoH forwarder is for tunneled names only.

```
/ip address add address=203.0.113.42/24 interface=ether1
/ip route add dst-address=0.0.0.0/0 gateway=203.0.113.1
/ip dns set servers=192.0.2.1,192.0.2.2
```

Check the ISP resolvers did not end up in `to_vpn_list`, or the router's own
queries get routed into the tunnel:

```
/ip firewall address-list print where list=to_vpn_list address=192.0.2.1
```

## Migrating a router that predates the split DNS flow

Routers imported from an older `fresh-router.rsc` send *all* DoH through the tunnel.
On each one:

```
/ip dhcp-client set [find interface=ether1] use-peer-dns=yes
/ip dns forwarders add name=vpn-doh doh-servers=https://dns.google/dns-query verify-doh-cert=yes
/ip dns set use-doh-server=""
/ip dns static add name=core.telegram.org type=FWD forward-to=vpn-doh comment="mtvpn:tg-fetch"
/ip firewall address-list add list=to_vpn_list address=core.telegram.org comment="mtvpn:tg-fetch"
/ip dns cache flush
```

then, from your workstation, rewrite every FWD entry with `forward-to=`:

```sh
./mtvpn.py -c mtvpn.yaml update
```
