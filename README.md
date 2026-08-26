# mtvpn

Selective-VPN domain routing on MikroTik RouterOS 7. Domains for the services you
pick go through your VPN gateway, everything else goes direct.

Two parts, with a clean split of ownership:

- `fresh-router.rsc` — one-shot `/import` template for a factory-fresh router.
- `mtvpn.py` — python3 CLI that fetches domain lists and fills that address-list
  over SSH.

## Domain sources

Every service names its source explicitly. Nothing is inferred, and one source is
never silently substituted for the other:

| entry | source |
|---|---|
| `iplist:youtube.com` | [iplist.opencck.org](https://github.com/rekryt/iplist) — a **site** |
| `iplist:apple` | iplist — a **group** (every site in it, here all 10 Apple ones) |
| `iplist:beta:cloudflare.com` | iplist with the portal pinned (`main`/`beta`/`russia`) |
| `v2fly:anthropic` | [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community) |
| `anthropic` | same as `v2fly:anthropic` — a bare name is the v2fly alias |
| `https://…` | a raw URL in either format — including your own list of domains |
| `mine=https://…` | the same, with the router tag named explicitly |

### Your own domain list

Any URL serving one domain per line works as a source — that format is a subset of
v2fly's, so no prefix or conversion is needed:

```sh
./mtvpn.py -n add https://files.example.com/tunneled-domains.txt   # dry-run first
./mtvpn.py add https://files.example.com/tunneled-domains.txt
```

The tag comes from the URL's last path segment minus its extension
(`tunneled-domains`), so `update` re-reads the URL and `remove tunneled-domains`
clears it. Two lists whose filenames match would collide under one tag, so name them
instead — `mine=https://…` makes the tag `mine` whatever the URL says. `full:`,
`domain:` and `include:` lines are honoured if you use them; `regexp:`/`keyword:`
lines are reported as skipped, since RouterOS cannot express them. `#` starts a
comment anywhere in the line, so a list can be annotated:

```
# work stuff
example.com        # only the apex is needed, subdomains match too
full:exact.example.org
```

Since these lists change more often than the curated upstreams, `update --urls-only`
refreshes just them and leaves the iplist/v2fly services untouched.

iplist spreads its catalog over three portals (`main`, `beta`, `russia`) with almost
no overlap, so `iplist:<selector>` tries each in turn and takes the first that has
it. `search` shows which one that is:

```sh
./mtvpn.py search claude          # both sources
./mtvpn.py search apple -s iplist # one source

iplist:apple              beta    group  10 site(s)
iplist:apple.com          beta    site   (apple)
iplist:apple@icloud.com   beta    site   (apple)
v2fly:apple
```

The router tag is the selector **without** its prefix, so `v2fly:youtube` still owns
the `comment=youtube` entries a bare `youtube` installed. Switching a service to a
differently-named selector (`v2fly:chesscom` → `iplist:chess.com`) changes the tag,
so clear the old one out:

```sh
./mtvpn.py domains chesscom       # compare: bare name prints every source that has it
./mtvpn.py remove chesscom        # drop the old tag's entries first
./mtvpn.py add iplist:chess.com   # then install under the new one
```

## Hosted service lists

The set of services can live on a server instead of in every router's config. A
service list is a plain text file, **one selector per line** — exactly what
`services:` holds, so a `services:` block can be pasted in as-is (leading `- ` is
stripped). `#` comments work at line start or after whitespace, which leaves raw-URL
entries intact:

```
v2fly:youtube
v2fly:telegram    # 21 domains
iplist:claude.ai
```

Point a config at it and every `update` applies whatever the file says today:

```yaml
service_lists:
  - https://files.example.com/mtvpn-tunneled.txt
services:
  - v2fly:anthropic     # this router only, on top of the list
```

```sh
# install everything the list carries and record the URL in service_lists:
./mtvpn.py add -l https://files.example.com/mtvpn-tunneled.txt

./mtvpn.py update              # config services + every list, re-fetched
./mtvpn.py update --urls-only  # only your own domain lists (raw URLs), not iplist/v2fly
./mtvpn.py update --prune      # ...and drop router services the lists no longer name
./mtvpn.py remove -l https://files.example.com/mtvpn-tunneled.txt   # list and its services

# one-off, without touching the config
./mtvpn.py -c mtvpn-hex.yaml update -l ./tunneled.txt
```

`-l` takes a URL or a local path and is repeatable. `add`/`remove` record and
forget the URL in `service_lists:`; `update` never edits the config, so a one-off
`-l` stays one-off. Services named in both the config and a list are installed
once, and `add`ing one a list already carries won't duplicate it into `services:`.

`--urls-only` narrows the refresh to services whose source is a raw URL — your own
domain lists, which you edit far more often than iplist or v2fly change. It cannot be
combined with `--prune`, which would then sweep every service it skipped.

`--prune` removes every service tag on the router that the effective set no longer
names — including ones installed by hand. It only applies to a full `update` (no
service arguments), and it leaves the router's own `telegram-cidr` entries alone.

## Requirements

- A router set up from `fresh-router.rsc` (or the same rules by hand — see below)
- RouterOS 7.x, SSH key auth
- python3, no pip installs

> LAN clients must use **the router as their only DNS server** — otherwise
  subdomain coverage silently breaks. For ex. if you use tailscale, it would work only if `--accept-dns=false` is set and host uses router as DNS in /etc/resolv.conf.

### Tunneled DNS

`fresh-router.rsc` sends **every** query the router makes through the tunnel. A DoH
query is TLS on 443, so the firewall cannot tell one name from another — there is
no per-query routing decision to be had, only a per-address one. So there is one
resolver, `dns.google`, pinned by a static A record and put into `to_vpn_list` by
hostname: the `mtvpn:conn-out` / `mtvpn:route-out` rules already tunnel
router-originated traffic to anything listed, so no extra rule is involved.

Answers are ordinary real A records — no fake-ip, no synthesised ranges — so the
`address-list=` mechanism is untouched, and mtvpn's service entries keep doing
exactly one job: putting the addresses they resolve into `to_vpn_list`. If the
container dies, `check-gateway=ping` withdraws the route and DNS falls back to
the WAN along with everything else — an inactive route in a marked table is a
lookup miss, and RouterOS retries a miss in `main`.

That fallback does *not* cover a container that is running while its tunnel is
dead: the veth still answers ping, so the route stays active and DoH dies inside
the tunnel. Since `/ip dns` carries no plain `servers=`, that takes the whole
LAN's DNS with it. Two netwatch probes cover the two cases — `mtvpn:watch-gw` on
the veth, and `mtvpn:watch-tunnel` opening TCP to `8.8.8.8:443` through the
tunnel — and both restart mihomo.

The trade-off is deliberate: names resolve from the exit node's vantage point
whether they are tunneled or not, so CDN answers for *direct* traffic are
geolocated to the exit node too. In exchange the router has one DNS path instead
of two, and an ISP filtering your resolver on the WAN (ICMP admin-prohibited on
`8.8.8.8:443` is a real thing) can no longer see it.

## Setting up a new router

Open `fresh-router.rsc` file and follow comments. Two things it does that affect
the rest of this README:

- **IPv6 is disabled** (takes effect on reboot). The selective-routing path is
  IPv4-only, so a dual-stack client would otherwise reach an AAAA-capable service
  direct over the WAN, silently bypassing the tunnel.
- Firewall and mangle rules match on the interface lists `LANiface`/`WANiface`
  rather than on the LAN bridge.

Everything the routing needs lives there, so `mtvpn.yaml` only has to name the
address-list to fill. To use mtvpn against a router set up some other way, give it
the equivalent of the template's `mtvpn:*` rules and point `list:` at whatever
address-list they match on.

## Config

`mtvpn.yaml`, or one file per router selected with `-c`:

```yaml
# full ssh command for reaching the router; anything ssh accepts
ssh: ssh -J jumphost 10.230.1.1
# optional: override the scp command derived from ssh: for the /import fast
# path (leave empty to auto-derive). {local}/{remote} are substituted.
# scp: scp -J jumphost {local} 10.230.1.1:{remote}
# the address-list the router routes through the tunnel — the one router-side
# name mtvpn needs. fresh-router.rsc creates it and the rules that act on it.
list: to_vpn_list
# hosted service lists (URLs or paths), merged into services by update
service_lists:
  - https://files.example.com/mtvpn-tunneled.txt
# managed by add/remove, applied by update
services:
  - v2fly:anthropic
  - iplist:claude.ai
  - iplist:beta:cloudflare.com
```

## Usage

```sh
# add services — also appends them to the config
./mtvpn.py add v2fly:anthropic iplist:chatgpt.com
./mtvpn.py add 'https://iplist.opencck.org/?format=text&data=domains&wildcard=1&site=youtube.com'

./mtvpn.py add -l https://files.example.com/mtvpn-tunneled.txt  # a hosted service list

./mtvpn.py update                  # re-fetch upstream lists, refresh everything
./mtvpn.py update --urls-only      # refresh only your own raw-URL domain lists
./mtvpn.py remove netflix
./mtvpn.py list -v                 # what's installed on the router, by service

# other router
./mtvpn.py -c mtvpn-hex.yaml add v2fly:openai

# no router needed
./mtvpn.py -n add v2fly:anthropic  # dry-run: print the RouterOS commands
./mtvpn.py render iplist:claude.ai > claude.rsc   # for manual /import
./mtvpn.py search google           # selectors matching "google", both sources
./mtvpn.py domains openai          # domains a service resolves to
```

`render`, `domains` and `search` never touch the router; everything else takes
`-r "ssh <...>"` to override the config's `ssh:`.

`add`/`update` are idempotent: entries tagged with the service comment are
replaced wholesale, and pre-existing *untagged* entries for the same domains are
adopted rather than duplicated.
