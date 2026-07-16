# mtvpn

Selective-VPN domain routing on MikroTik RouterOS 7. Domains from
[v2fly/domain-list-community](https://github.com/v2fly/domain-list-community)
(`anthropic`, `openai`, `telegram`, `netflix`, …) go through your VPN gateway,
everything else goes direct.

Two parts:

- `fresh-router.rsc` — one-shot bootstrap template for a factory-fresh router
  (bridges, DHCP, DoH DNS, firewall, NAT, mihomo container, VPN routing infra).
- `mtvpn.py` — python3 CLI (stdlib only) that fetches the lists and pushes
  DNS + address-list entries to the router over SSH.

## Requirements

- RouterOS 7.x, SSH key auth (`ssh <router>` works without a password)
- python3, no pip installs
- A VPN gateway reachable from the router as a next-hop IP (the mihomo
  container from `fresh-router.rsc`, a WireGuard peer, …)
- LAN clients must use **the router as their only DNS server** — otherwise
  subdomain coverage silently breaks

## Setting up a new router

1. Edit the `PARAMETERS` section at the top of `fresh-router.rsc`: LAN subnet,
   WAN port, subscription URL, timezone, interface names, container subnet, and
   `vpnList`/`vpnTable`/`vpnMark` (keep those equal to `list:`/`table:`/`mark:`
   in this router's config below).
2. Do the one-time `PREREQUISITES` from the file header: reset config, enable
   device-mode containers, format the USB disk, import your SSH key.
3. Upload and `/import fresh-router.rsc`, then follow `AFTER IMPORT`.
4. Create a config for it and add services (below).

The template creates the routing infra with mtvpn-compatible `comment=` tags,
so `bootstrap` is *not* needed after import.

Router DoH goes direct to WAN on purpose: routing it through the VPN container
caused intermittent wrong DNS answers and made DNS depend on the container.

## Config

`mtvpn.yaml`, or one file per router selected with `-c` (they are gitignored —
never commit router addresses or subscription URLs):

```yaml
# full ssh command for reaching the router; anything ssh accepts
ssh: ssh -J jumphost 10.230.1.1
# next-hop IP of the VPN gateway (container veth / tunnel peer)
gateway: 192.168.89.2
list: to_vpn_list
table: to_vpn_table
mark: to_vpn_mark
lan_list: LAN
# managed by add/remove, applied by update/bootstrap
services:
  - anthropic
  - openai
```

## Usage

```sh
# add services (v2fly name or raw URL) — also appends them to the config
./mtvpn.py add anthropic openai
./mtvpn.py add https://raw.githubusercontent.com/v2fly/domain-list-community/refs/heads/master/data/openai

./mtvpn.py update                  # re-fetch upstream lists, refresh everything
./mtvpn.py remove netflix
./mtvpn.py list -v                 # what's installed on the router, by service

# other router
./mtvpn.py -c mtvpn-hex.yaml add openai

# no router needed
./mtvpn.py -n add anthropic        # dry-run: print the RouterOS commands
./mtvpn.py render anthropic > anthropic.rsc   # for manual /import
./mtvpn.py search google           # v2fly service names matching "google"
./mtvpn.py domains openai          # domains a service resolves to

# router without fresh-router.rsc: install the routing infra first
./mtvpn.py bootstrap --fix-fasttrack
```

`render`, `domains` and `search` never touch the router; everything else takes
`-r "ssh <...>"` to override the config's `ssh:`.

`add`/`update` are idempotent: entries tagged with the service comment are
replaced wholesale, and pre-existing *untagged* entries for the same domains are
adopted rather than duplicated.

Re-run `update` when upstream lists change, or cron it on the machine with SSH
access:

```
0 6 * * 1  cd /path/to/mtvpn && ./mtvpn.py update
```

## Notes / limits

- Routing **fails open**: the `to_vpn_table` default route uses
  `check-gateway=ping`, so a dead gateway means traffic falls back to direct
  WAN, not a blackhole.
- `--fix-fasttrack` adds `connection-mark=!to_vpn_mark` to existing fasttrack
  rules — without it fasttrack bypasses mangle for established connections and
  leaks them to direct WAN.
- `regexp:` and `keyword:` rules from v2fly lists are skipped (RouterOS can't
  express them) and printed so you can add equivalents manually.
- IPv4-focused; if your LAN has working IPv6, listed sites may bypass the VPN
  over v6 unless you filter it similarly.
- Very large services (e.g. `google`) create hundreds of DNS static entries;
  keep the service set intentional.
- The `@ads`/`@cn` v2fly attributes are ignored — all domains of a service are
  included.
