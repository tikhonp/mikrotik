# mtvpn

Bootstrap and manage **selective-VPN domain routing** on MikroTik RouterOS 7,
with domain lists taken from
[v2fly/domain-list-community](https://github.com/v2fly/domain-list-community)
(the `geosite` data: `anthropic`, `openai`, `telegram`, `netflix`, …).

You give it a list of service names (or raw URLs to files in the same format);
traffic to every domain of those services is routed through your VPN gateway.
Everything else goes direct.

## Requirements

- RouterOS **7.x**
- SSH key auth to the router (`ssh <router>` must work without a password)
- python3 (stdlib only, no pip installs)
- A working VPN gateway reachable from the router as a next-hop IP —
  a container (mihomo/xray/sing-box on a veth), a WireGuard peer, etc.
  mtvpn routes *to* it; setting the gateway itself up is out of scope.
- LAN clients must use **the router as their only DNS server**
  (`/ip dhcp-server network set ... dns-server=<router>`,
  `/ip dns set allow-remote-requests=yes`). If clients resolve elsewhere,
  subdomain coverage silently breaks.

## How it works

For each domain of a service, two entries are created (both tagged
`comment=<service>` so they can be refreshed/removed as a unit):

1. **DNS static FWD entry** with `match-subdomain=yes` and
   `address-list=to_vpn_list` — every name a client resolves under that domain
   drops its IPs into the list (dynamic entries, renewed per query).
   `full:` domains from v2fly get `match-subdomain=no` (exact match).
2. **Firewall address-list hostname entry** — the router itself keeps the apex
   A records resolved continuously; survives DNS cache flushes and TTL expiry.

Routing infra (created once by `bootstrap`, idempotent):

- routing table `to_vpn_table`
- mangle: connections to listed IPs get `to_vpn_mark`
  (prerouting limited to `in-interface-list=LAN` so gateway-originated traffic
  can't loop back into the gateway), then `mark-routing` → `to_vpn_table`
- default route `0.0.0.0/0 → <gateway>` in `to_vpn_table` with
  `check-gateway=ping` — if the gateway dies, routing **fails open** to the
  main table (direct WAN) instead of blackholing
- mangle `change-mss` (comment `mtvpn:mss-clamp`): TCP MSS on `to_vpn_mark`
  connections is clamped to 1360, so tunnel encapsulation overhead (VLESS/Reality
  shrinks the effective path MTU) can't stall full-size segments. Matching by
  connection-mark clamps both the SYN and SYN-ACK, covering both directions.
- `--fix-fasttrack` adds `connection-mark=!to_vpn_mark` to existing fasttrack
  rules (fasttrack would otherwise bypass mangle for established connections
  and leak them to direct WAN). Verify with
  `/ip firewall filter print where action=fasttrack-connection` afterwards.

`regexp:` and `keyword:` rules from v2fly lists are skipped (RouterOS cannot
express them); skips are printed so you can add equivalents manually.

## Usage

Create `mtvpn.yaml` (or one file per router, selected with `-c`):

```yaml
# full ssh command for reaching the router (key auth required)
ssh: ssh -J jumphost 10.230.1.1
# next-hop IP of the VPN gateway (container veth / tunnel peer)
gateway: 192.168.89.2
list: to_vpn_list
table: to_vpn_table
mark: to_vpn_mark
lan_list: LAN
# v2fly service names or raw URLs; managed by `add`/`remove`, applied by `update`/`bootstrap`
services:
  - anthropic
  - openai
```

```sh
# fresh router: install infra + all services from config
./mtvpn.py bootstrap --fix-fasttrack

# add services (v2fly name or raw URL) — also appends them to the config
./mtvpn.py add anthropic
./mtvpn.py add https://raw.githubusercontent.com/v2fly/domain-list-community/refs/heads/master/data/openai

# re-fetch upstream lists and refresh everything from config
./mtvpn.py update

./mtvpn.py remove netflix
./mtvpn.py list -v                 # what's installed on the router, by service

# inspect without touching the router
./mtvpn.py -n add anthropic        # dry-run: print the RouterOS commands
./mtvpn.py render anthropic > anthropic.rsc   # for manual /import
```

The router is addressed by a full ssh command string — `ssh: ssh 10.210.10.1`
in the config, or anything ssh accepts, e.g. `ssh: ssh -J jumphost 10.220.1.1`
for a router behind a jump host. Everything except `render` takes
`-r "ssh <...>"` to override the config, and one checkout manages several
routers — keep one config file per router (`./mtvpn.py -c mtvpn-hex.yaml add
openai`). Legacy JSON configs still load; saves are written as YAML.

`add`/`update` are idempotent: entries tagged with the service comment are
replaced wholesale, and pre-existing *untagged* entries for the same domains
are adopted (removed and re-added with the tag) instead of duplicated.

## Setting up a brand-new router

`fresh-router.rsc` is a full bootstrap template for a factory-fresh MikroTik:
bridges, DHCP (configurable LAN subnet), DoH DNS, firewall, NAT, the mihomo
container, the VPN routing infra (with mtvpn-compatible rule comments, so
`bootstrap` is not needed), a container watchdog, and the Telegram IP-range
updater script. Router DoH goes direct to WAN on purpose: routing it through
the VPN container caused intermittent wrong DNS answers and made DNS depend
on the container being up.

1. Edit the `PARAMETERS` section at the top: LAN subnet prefix, WAN port,
   subscription URL, timezone, interface names, container subnet, and the
   selective-VPN names (`vpnList` / `vpnTable` / `vpnMark`) — keep those three
   equal to `list:` / `table:` / `mark:` in this router's `mtvpn` config, or
   `add`/`update` won't line up with the rules this template creates.
2. Do the one-time `PREREQUISITES` listed in the file header (reset config,
   install the container package, enable device-mode container, format the
   USB disk, import your SSH key).
3. Upload and `/import fresh-router.rsc`.
4. Create a config file for the new router and add services:
   `./mtvpn.py -c mtvpn-newrouter.yaml add anthropic openai youtube`

## Keeping lists fresh

Upstream lists change occasionally. Re-run `./mtvpn.py update` when needed, or
add a cron entry on the machine that has SSH access:

```
0 6 * * 1  cd /path/to/mtvpn && ./mtvpn.py update
```

## Notes / limits

- IPv4-focused; if your LAN has working IPv6, listed sites may bypass the VPN
  over v6 unless you filter it similarly.
- Very large services (e.g. `google`) create hundreds of DNS static entries;
  RouterOS handles thousands, but keep the service set intentional.
- The `@ads`/`@cn` v2fly attributes are ignored — all domains of a service are
  included.
