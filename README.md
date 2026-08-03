# mtvpn

Selective-VPN domain routing on MikroTik RouterOS 7. Domains for the services you
pick go through your VPN gateway, everything else goes direct.

Two parts:

- `fresh-router.rsc` — one-shot bootstrap template for a factory-fresh router.
- `mtvpn.py` — python3 CLI that fetches the lists and pushes
  DNS + address-list entries to the router over SSH.

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
| `https://…` | a raw URL in either format |

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

## Requirements

- RouterOS 7.x, SSH key auth
- python3, no pip installs
- A VPN gateway reachable from the router as a next-hop IP

> LAN clients must use **the router as their only DNS server** — otherwise
  subdomain coverage silently breaks. For ex. if you use tailscale, it would work only if `--accept-dns=false` is set and host uses router as DNS in /etc/resolv.conf.

## Setting up a new router

Open `fresh-router.rsc` file and follow comments. Two things it does that affect
the rest of this README:

- **IPv6 is disabled** (takes effect on reboot). The selective-routing path is
  IPv4-only, so a dual-stack client would otherwise reach an AAAA-capable service
  direct over the WAN, silently bypassing the tunnel.
- Firewall and mangle rules match on the interface lists `LANiface`/`WANiface`,
  so that router's config needs `lan_list: LANiface` — see below.

## Config

`mtvpn.yaml`, or one file per router selected with `-c`:

```yaml
# full ssh command for reaching the router; anything ssh accepts
ssh: ssh -J jumphost 10.230.1.1
# optional: override the scp command derived from ssh: for the /import fast
# path (leave empty to auto-derive). {local}/{remote} are substituted.
# scp: scp -J jumphost {local} 10.230.1.1:{remote}
# next-hop IP of the VPN gateway (container veth / tunnel peer)
gateway: 192.168.89.2
list: to_vpn_list
table: to_vpn_table
mark: to_vpn_mark
# interface *list* (not the LAN bridge) that the VPN mangle rules match on.
# fresh-router.rsc creates LANiface/WANiface; mtvpn's built-in default is "LAN",
# which on that router is the bridge. Only `bootstrap` reads this key.
lan_list: LANiface
# managed by add/remove, applied by update/bootstrap
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

./mtvpn.py update                  # re-fetch upstream lists, refresh everything
./mtvpn.py remove netflix
./mtvpn.py list -v                 # what's installed on the router, by service

# other router
./mtvpn.py -c mtvpn-hex.yaml add v2fly:openai

# no router needed
./mtvpn.py -n add v2fly:anthropic  # dry-run: print the RouterOS commands
./mtvpn.py render iplist:claude.ai > claude.rsc   # for manual /import
./mtvpn.py search google           # selectors matching "google", both sources
./mtvpn.py domains openai          # domains a service resolves to

# router without fresh-router.rsc: install the routing infra first.
# Safe to run against a fresh-router.rsc router too — with lan_list: LANiface
# set it finds every rule the template already made and adds nothing.
./mtvpn.py bootstrap --fix-fasttrack
```

`render`, `domains` and `search` never touch the router; everything else takes
`-r "ssh <...>"` to override the config's `ssh:`.

`add`/`update` are idempotent: entries tagged with the service comment are
replaced wholesale, and pre-existing *untagged* entries for the same domains are
adopted rather than duplicated.
