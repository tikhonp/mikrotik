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

Use [`fresh-router.rsc`](fresh-router.rsc) as a template, modify params, maybe add static leases at the end, maybe static WAN address, rules to restrict IoT devices to LAN-only and a guest VLAN, then `/import` it. 

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

### Guest VLAN (UniFi APs)

```
/interface vlan add name=guest interface=LAN vlan-id=30
/interface list add name=GUESTiface
/interface list member add list=GUESTiface interface=guest
/ip address add address=10.230.30.1/24 interface=guest
/ip pool add name=guest_pool ranges=10.230.30.20-10.230.30.254
/ip dhcp-server add name=dhcp-guest interface=guest address-pool=guest_pool lease-time=1h disabled=no
/ip dhcp-server network add address=10.230.30.0/24 gateway=10.230.30.1 dns-server=10.230.30.1
/ip firewall address-list add list=guest_nets address=10.230.30.0/24
```

Input: DNS on the guest address only, then drop the rest. Both go before "allow
ping to the router", which has no interface match. `dst-address=` matters: the
input chain accepts any of the router's addresses, so without it a guest could
query DNS on `10.230.1.1` too.

```
/ip firewall filter add action=accept chain=input comment="guest: DNS only" \
    in-interface-list=GUESTiface src-address-list=guest_nets dst-address=10.230.30.1 \
    protocol=udp dst-port=53 place-before=[find comment="allow ping to the router"]
/ip firewall filter add action=accept chain=input comment="guest: DNS only" \
    in-interface-list=GUESTiface src-address-list=guest_nets dst-address=10.230.30.1 \
    protocol=tcp dst-port=53 place-before=[find comment="allow ping to the router"]
/ip firewall filter add action=drop chain=input comment="guest: nothing else on the router" \
    in-interface-list=GUESTiface place-before=[find comment="allow ping to the router"]
```

Forward: internet only. VPN-routed guest traffic leaves through the `container`
bridge, not the WAN, so `connection-mark=!to_vpn_mark` exempts it. That still
drops unmarked traffic to mihomo itself (`192.168.89.2`). The explicit `LANiface`
drop covers a tunneled name resolving to a LAN address while the VPN route is down
(the lookup then falls back to `main`).

```
/ip firewall filter add action=drop chain=forward comment="guest: drop spoofed sources" \
    in-interface-list=GUESTiface src-address-list=!guest_nets \
    place-before=[find comment="jump to ICMP filters"]
/ip firewall filter add action=drop chain=forward comment="guest: never to LAN" \
    in-interface-list=GUESTiface out-interface-list=LANiface \
    place-before=[find comment="jump to ICMP filters"]
/ip firewall filter add action=drop chain=forward comment="guest: internet and VPN only" \
    in-interface-list=GUESTiface out-interface-list=!WANiface connection-mark=!to_vpn_mark \
    place-before=[find comment="jump to ICMP filters"]
```

Selective VPN for guests: the template's `mtvpn:conn-lan` / `mtvpn:route-pre` match
`LANiface` only, so the guest VLAN gets its own pair. MSS clamping (by
connection-mark) and masquerade (by WAN out list) already cover it, and `mtvpn.py`
never touches mangle.

```
/ip firewall mangle add chain=prerouting action=mark-connection connection-mark=no-mark \
    dst-address-list=to_vpn_list in-interface-list=GUESTiface new-connection-mark=to_vpn_mark \
    passthrough=yes comment="mtvpn:conn-guest"
/ip firewall mangle add chain=prerouting action=mark-routing connection-mark=to_vpn_mark \
    in-interface-list=GUESTiface new-routing-mark=to_vpn_table passthrough=no \
    comment="mtvpn:route-guest"
```

Last, carry VLAN 30 tagged on the bridge itself and on every port an AP (or a switch
in front of APs) hangs off - adjust the port list. The main LAN stays untagged
(pvid 1; RouterOS adds that entry on its own). VLAN filtering then drops frames
tagged with any other VLAN; the RB5009 switch chip still offloads it.

```
/interface bridge vlan add bridge=LAN vlan-ids=30 tagged=LAN,ether2,ether3
/interface bridge set LAN vlan-filtering=yes
```

On the UniFi side (no UniFi gateway, so the controller only tags):

- Settings → Networks → new network, router *Third-party Gateway* (*VLAN Only* in
  older versions), VLAN ID `30`. Leave the DHCP and gateway settings to the MikroTik.
- Settings → WiFi → new SSID on that network, with **Client Device Isolation** on
  (blocks guest-to-guest, which never reaches the router).
- A UniFi switch between the router and the APs must carry VLAN 30 tagged on those
  ports (the default *Allow All* port profile does).
