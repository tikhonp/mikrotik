# mtvpn

Selective-VPN domain routing on MikroTik RouterOS 7.24.5+. Domains for the services you
pick go through your VPN gateway, everything else goes direct.

Two parts:

- `fresh-router.rsc` — one-shot `/import` template for a factory-fresh router.
- `mtvpn.py` — python3 CLI (stdlib only) that fetches domain lists and fills that
  address-list over SSH.

> LAN clients must use **the router as their only DNS server**, or subdomain
> coverage silently degrades. With tailscale that means `--accept-dns=false` and
> the router in the host's `/etc/resolv.conf`.

## Domain sources

Every service names its source explicitly.

| entry | source |
|---|---|
| `iplist:youtube.com` | [iplist.opencck.org](https://github.com/rekryt/iplist) — a **site** |
| `iplist:apple` | iplist — a **group** (every site in it) |
| `iplist:beta:cloudflare.com` | iplist with the portal pinned (`main`/`beta`/`russia`) |
| `v2fly:anthropic` | [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community) |
| `anthropic` | same as `v2fly:anthropic` — a bare name is the v2fly alias |
| `https://…` | a raw URL in either format — including your own list of domains |
| `mine=https://…` | the same, with the router tag named explicitly |

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
removes every service tag on the router the effective set no longer names;

## Config

`mtvpn.yaml` holds only *what* to tunnel, so one file serves every router:

```yaml
service_lists:
  - https://files.example.com/mtvpn-tunneled.txt
services:
  - v2fly:anthropic
  - iplist:claude.ai
```

## Reaching the router

Without `-r`, mtvpn takes the `.1` of the network this machine is on: on `10.220.1.57`
it talks to `10.220.1.1`, and prints which router it picked. `-r` names one instead:

```sh
./mtvpn.py -r 10.230.1.1 list                  # ssh 10.230.1.1
./mtvpn.py -r 100.64.0.1:10.230.1.1 list       # ssh -J 100.64.0.1 10.230.1.1
```

`JUMPHOST:HOST` becomes ssh's (and scp's) `-J`, which is how you reach a router you
are not on the LAN of. Key auth only — mtvpn runs ssh with `BatchMode=yes`.

## Usage

```sh
./mtvpn.py add v2fly:anthropic iplist:chatgpt.com
./mtvpn.py add -l https://files.example.com/mtvpn-tunneled.txt

./mtvpn.py update                  # re-fetch upstream lists, refresh everything
./mtvpn.py update --urls-only      # refresh only your own raw-URL domain lists
./mtvpn.py update --prune          # ...and drop services the lists no longer name
./mtvpn.py remove netflix
./mtvpn.py list -v                 # what's installed on the router, by service

./mtvpn.py -r 10.230.1.1 add v2fly:openai       # another router

# no router needed
./mtvpn.py -n add v2fly:anthropic  # dry-run: print the RouterOS commands
./mtvpn.py search google
./mtvpn.py domains openai
```

`domains`, `search` and `shadowrocket` never touch the router; everything else uses the discovered
router unless `-r` names one.

`add`/`update` are idempotent: entries tagged with the service comment are replaced
wholesale, and pre-existing *untagged* entries for the same domains are adopted rather
than duplicated. Entries commented `mtvpn:*` are infrastructure pins and are never
adopted, removed or pruned.

## Shadowrocket (iOS, off the LAN)

`shadowrocket` renders the same service set as a [Shadowrocket](https://apps.apple.com/app/shadowrocket/id932747118)
config, so a phone away from home tunnels the same domains. It takes a hand-written
base config (`[General]`, DNS, upstream `RULE-SET`s…) and appends one `# <service>`
block of `DOMAIN-SUFFIX`/`DOMAIN` rules per service to its `[Rule]` section, before
the base's `FINAL` line (or closes with `FINAL,DIRECT` if the base has none). A name
another service's suffix already covers is dropped, so the file stays lean.

```yaml
shadowrocket_base: https://files.example.com/shadowrocket-base.conf   # URL or path
shadowrocket_upload: https://files.example.com/phone/shadowrocket.conf
shadowrocket_user: me          # optional
shadowrocket_password: secret  # or env MTVPN_UPLOAD_PW (MTVPN_UPLOAD_USER for the login)
```

```sh
./mtvpn.py shadowrocket                     # upload if configured, else write shadowrocket.conf
./mtvpn.py shadowrocket -o sr.conf          # upload and keep a local copy
./mtvpn.py shadowrocket --no-upload -o sr.conf
./mtvpn.py -n shadowrocket                  # print it instead
./mtvpn.py shadowrocket v2fly:openai        # only the named services
```

The upload is an HTTP `PUT` to a [copyparty](https://github.com/9001/copyparty)
server with `Replace: 1`. Point `shadowrocket_upload` at the file's real path, not a
`/share/…` link (shares are read-only), then subscribe Shadowrocket to the share.
With `shadowrocket_user` set it authenticates with HTTP Basic auth `user:password`,
which copyparty accepts with or without `--usernames`; with only a password it sends
copyparty's `PW` header. The account needs **write and delete** access to the
folder: without delete, copyparty keeps the old file and stores the upload under a
new name, which mtvpn reports as an error.

## Setting up a new router

Use [`fresh-router.rsc`](fresh-router.rsc) as a template, modify params, maybe add static leases at the end, maybe static WAN address and rules to restrict IoT devices to LAN-only, then `/import` it. 

### Static WAN address (no ISP DHCP)

Disable the DHCP client:

```
/ip dhcp-client disable dhcp1
```

Set a static address, route and DNS servers:

```
/ip address add address=203.0.113.42/24 interface=ether1
/ip route add dst-address=0.0.0.0/0 gateway=203.0.113.1
/ip dns set servers=192.0.2.1,192.0.2.2
```

### Restricting internet access for IoT devices

Give the devices static leases so their addresses are stable, list those addresses,
and drop anything they send outside the LAN:

```
/ip dhcp-server lease add address=10.230.1.50 mac-address=AA:BB:CC:DD:EE:01 
/ip firewall address-list add list=iot-no-wan address=10.230.1.50
```
 
Than add a `forward` rule to drop any traffic from that list that is not going to the LAN:

```
/ip firewall filter add action=drop chain=forward comment="IoT: LAN only" \
    src-address-list=iot-no-wan dst-address-list=!lan_nets \
    place-before=[find chain=forward comment="jump to ICMP filters"]
```
