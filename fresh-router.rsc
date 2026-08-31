# fresh-router.rsc - bootstrap a fresh MikroTik with selective-VPN routing
#
# USAGE
#   1. Edit the PARAMETERS section below.
#   2. Complete the PREREQUISITES (manual, once per device).
#   3. Upload this file and run:  /import fresh-router.rsc
#   4. From your workstation:     ./mtvpn.py -c <router>.yaml add anthropic youtube ...
#
# PREREQUISITES (before import)
#   - Clean config:      /system reset-configuration no-defaults=yes skip-backup=yes
#   - Container package: /system package print   must list an enabled "container".
#                        It ships in the "Extra packages" archive (RB5009 = arm64),
#                        not the routeros bundle. Without it RouterOS rejects the
#                        WHOLE import and the router ends up with no LAN address.
#   - Device mode:       /system/device-mode/update mode=advanced container=yes scheduler=yes fetch=yes
#   - USB disk:          /disk format usb1 file-system=ext4
#   - SSH key:           /user ssh-keys import public-key-file=key.pub user=...
#                        /ip ssh set password-authentication=no
#   - Disable admin:     /user disable admin

# PARAMETERS
# LAN /24 prefix (no trailing dot). Router gets .1, DHCP hands out .20-.254
:local lanNet "10.230.1"
# WAN port (gets its address, and the LAN's DNS servers, via DHCP client)
:local wanIface "ether1"
# mihomo subscription URL (SUB1 env of the container)
:local subUrl "https://files....t"
:local image "registry-1.docker.io/wiktorbgu/mihomo-mikrotik:latest"
:local timeZone "Europe/Moscow"

# interface names
:local lanIface "LAN"
:local containerIface "container"
:local vethName "vless"

# interface *lists* names
:local lanList "LANiface"
:local wanList "WANiface"

# selective-VPN routing names. $vpnList must match `list:` in mtvpn.yaml.
:local vpnList "to_vpn_list"
:local vpnTable "to_vpn_table"
:local vpnMark "to_vpn_mark"

# container internal /24: router side = .1, mihomo veth = .2 = the VPN gateway
:local containerNet "192.168.89"
:local vpnGateway ($containerNet . ".2")

# DoH resolver for tunneled services only. $dohForwarder must match
# `doh_forwarder:` in mtvpn.yaml.
:local dohHost "dns.google"
:local dohIP "8.8.8.8"
:local dohForwarder "vpn-doh"

# PRECHECK device-mode.
:local containerOk true
:foreach need in={"container";"scheduler";"fetch"} do={
    :if ([/system device-mode get $need] != true) do={
        :set containerOk false
        :put ("!! device-mode blocks '" . $need . "' (mode=" . [/system device-mode get mode] . \
            ") - container setup will be SKIPPED")
        :log warning ("fresh-router: device-mode blocks " . $need)
    }
}
:if ($containerOk = false) do={
    :put "!! fix with: /system/device-mode/update mode=advanced container=yes scheduler=yes fetch=yes"
    :put "!! then confirm with the reset button / power cycle, and re-run this import."
}

# bridges & ports
/interface bridge add name=$lanIface
/interface bridge add name=$containerIface
:local lanPorts [/interface ethernet find where name!=$wanIface]
:foreach e in=$lanPorts do={
    /interface bridge port add bridge=$lanIface interface=[/interface ethernet get $e name]
}
# Without this the bridge borrows the MAC of the lowest-numbered *running* port,
# so it changes as ports go up and down and LAN clients must re-ARP their gateway.
:if ([:len $lanPorts] > 0) do={
    /interface bridge set [find name=$lanIface] auto-mac=no admin-mac=[/interface ethernet get [:pick $lanPorts 0] mac-address]
}
:put "stage: bridges+ports ok"

# Interface lists. The *bridge* is the member, not its ports: for routed traffic
# the IP firewall sees the bridge as in-interface. The container bridge is
# deliberately in neither list.
/interface list add name=$lanList
/interface list add name=$wanList
/interface list member add list=$lanList interface=$lanIface
/interface list member add list=$wanList interface=$wanIface
:put "stage: interface lists ok"

# Addressing / DHCP. The veth comes later, next to the container: it is the first
# command that can fail on a device without the container package, and LAN access
# must not depend on it.
/ip address add address=($lanNet . ".1/24") interface=$lanIface
/ip address add address=($containerNet . ".1/24") interface=$containerIface
/ip pool add name=dhcp_pool ranges=($lanNet . ".20-" . $lanNet . ".254")
/ip dhcp-server add address-pool=dhcp_pool interface=$lanIface name=dhcp1 disabled=no
/ip dhcp-server network add address=($lanNet . ".0/24") gateway=($lanNet . ".1") dns-server=($lanNet . ".1")
/ip dhcp-client add interface=$wanIface use-peer-dns=yes disabled=no

# DNS. Two paths: everything resolves via the ISP's DHCP-supplied servers over
# plain 53 out the WAN (no servers= here - use-peer-dns fills dynamic-servers),
# while mtvpn's per-domain FWD entries carry forward-to=$dohForwarder and resolve
# through the tunnel. The static A record pins the resolver to one address so the
# address-list entry further down can route it, and bootstraps the forwarder
# itself. verify-doh-cert stays set here: on 7.24.1 the forwarder's own copy of
# that property stores nil and verification is inherited from the global one.
/ip dns static add address=$dohIP name=$dohHost type=A
/ip dns static add address=($lanNet . ".1") name=router.lan type=A
/ip dns forwarders add name=$dohForwarder doh-servers=("https://" . $dohHost . "/dns-query") verify-doh-cert=yes
/ip dns set allow-remote-requests=yes verify-doh-cert=yes doh-max-concurrent-queries=200 doh-max-server-connections=40 doh-timeout=10s cache-size=16384KiB
:put "stage: addressing/DHCP/DNS ok"

/ip firewall address-list add list=lan_nets address=($lanNet . ".0/24")

# No /ip firewall raw rules on purpose: raw sits ahead of connection tracking, so
# every rule there is evaluated on every packet, FastTracked ones included.

# firewall filter. input is default-deny, so a tunnel or VLAN added later must
# join $lanList (or get its own accept) or its input is dropped.
/ip firewall filter add action=accept chain=input comment="established, related, untracked" connection-state=established,related,untracked
/ip firewall filter add action=drop chain=input comment="drop invalid" connection-state=invalid
# in-interface-list= as well as src-address-list=, else a WAN packet spoofing a
# LAN source address would be accepted. DHCP needs no rule: RouterOS handles it
# before the filter chain.
/ip firewall filter add action=accept chain=input comment="trusted LAN sources" in-interface-list=$lanList src-address-list=lan_nets
/ip firewall filter add action=accept chain=input comment="allow ping to the router" limit=5,10:packet protocol=icmp
/ip firewall filter add action=drop chain=input comment="default deny: anything not accepted above"
/ip firewall filter add action=fasttrack-connection chain=forward comment="FastTrack (skips VPN-marked)" connection-mark=no-mark connection-state=established,related
/ip firewall filter add action=accept chain=forward comment="Established, Related, Untracked" connection-state=established,related,untracked
# These drops deliberately do NOT log: internet background scanning fires them
# constantly, and a log write per packet turns a scan into self-inflicted load.
/ip firewall filter add action=drop chain=forward comment="Drop invalid" connection-state=invalid
/ip firewall filter add action=drop chain=forward comment="Drop incoming packets that are not NAT`ted" connection-nat-state=!dstnat connection-state=new in-interface-list=$wanList
/ip firewall filter add action=jump chain=forward comment="jump to ICMP filters" jump-target=icmp protocol=icmp
/ip firewall filter add action=drop chain=forward comment="Drop packets from LAN that do not have LAN IP" in-interface-list=$lanList src-address-list=!lan_nets
/ip firewall filter add action=accept chain=icmp comment="echo reply" icmp-options=0:0 limit=5,10:packet protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="net unreachable" icmp-options=3:0 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="host unreachable" icmp-options=3:1 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="host unreachable fragmentation required" icmp-options=3:4 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow echo request" icmp-options=8:0 limit=5,10:packet protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow time exceed" icmp-options=11:0 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow parameter bad" icmp-options=12:0 protocol=icmp
/ip firewall filter add action=drop chain=icmp comment="deny all other types"

/ip firewall nat add action=masquerade chain=srcnat comment="Masquerade LAN -> WAN" out-interface-list=$wanList
:put "stage: firewall ok"

# selective-VPN routing infrastructure (mtvpn-compatible comments)
/routing table add name=$vpnTable fib
/ip firewall mangle add chain=prerouting action=mark-connection connection-mark=no-mark dst-address-list=$vpnList in-interface-list=$lanList new-connection-mark=$vpnMark passthrough=yes comment="mtvpn:conn-lan"
/ip firewall mangle add chain=output action=mark-connection connection-mark=no-mark dst-address-list=$vpnList new-connection-mark=$vpnMark passthrough=yes comment="mtvpn:conn-out"
/ip firewall mangle add chain=prerouting action=mark-routing connection-mark=$vpnMark in-interface-list=$lanList new-routing-mark=$vpnTable passthrough=no comment="mtvpn:route-pre"
/ip firewall mangle add chain=output action=mark-routing connection-mark=$vpnMark new-routing-mark=$vpnTable passthrough=no comment="mtvpn:route-out"
# VLESS/Reality encapsulation shrinks the path MTU, so unclamped full-size
# segments stall. Match by connection-mark so SYN and SYN-ACK are both clamped.
/ip firewall mangle add chain=forward action=change-mss new-mss=1360 passthrough=yes protocol=tcp tcp-flags=syn connection-mark=$vpnMark tcp-mss=1361-65535 comment="mtvpn:mss-clamp"
# An inactive route in a marked table is a lookup miss, and a miss falls back to
# main - so a dead gateway fails open to the WAN.
/ip route add dst-address=0.0.0.0/0 gateway=$vpnGateway routing-table=$vpnTable check-gateway=ping comment="mtvpn:route"

# Two hosts the router itself must reach through the tunnel: the DoH resolver and
# the Telegram CIDR source. mtvpn:conn-out / mtvpn:route-out do the routing.
# By hostname, not address: RouterOS refuses a static address-list entry that
# duplicates a dynamic one in the same list, and 8.8.8.8 lands there dynamically
# on its own (ping2.ui.com, in the v2fly ubiquiti list, *is* 8.8.8.8) - a
# hostname entry merges into it instead of colliding. The mtvpn: comment is what
# keeps `mtvpn remove` and `update --prune` off them; the FWD entry carries no
# address-list= for the same reason.
/ip firewall address-list add list=$vpnList address=$dohHost comment="mtvpn:doh"
/ip dns static add name=core.telegram.org type=FWD forward-to=$dohForwarder comment="mtvpn:tg-fetch"
/ip firewall address-list add list=$vpnList address=core.telegram.org comment="mtvpn:tg-fetch"

# container + veth + watchdogs. One :do block: each step depends on the previous,
# and the usual failures (no container package, device-mode unconfirmed, usb1 not
# formatted) all surface here. on-error keeps the import going.
:if ($containerOk) do={
    :do {
        /interface veth add name=$vethName address=($containerNet . ".2/24") gateway=($containerNet . ".1")
        /interface bridge port add bridge=$containerIface interface=$vethName
        /container config set registry-url=https://registry-1.docker.io tmpdir=usb1/container-tmp layer-dir=usb1/container-tmp/layer
        /container envs add key=SUB1 list=mihomo value=$subUrl
        /container add remote-image=$image interface=$vethName envlists=mihomo dns=8.8.8.8,8.8.4.4 root-dir=usb1/container-tmp/docker/mihomo start-on-boot=yes
        :local restart "/container stop [find name~\"mihomo\"]; :delay 5s; /container start [find name~\"mihomo\"]; :log warning \"mihomo restarted: "
        # 1. container down: the veth stops answering. Probe the gateway itself -
        #    by then the VPN route is inactive and everything else fails open, so
        #    a probe aimed further out would be answered directly.
        /tool netwatch add host=$vpnGateway interval=1m timeout=2s type=simple comment="mtvpn:watch-gw" down-script=($restart . "gateway down\"")
        # 2. container up, tunnel dead: mihomo answers ping but forwards nothing,
        #    so check-gateway keeps the route active. $dohIP:443 is pinned into
        #    $vpnList above, so this probe rides the tunnel and fails on exactly
        #    the address and port DoH uses. tcp-conn, not simple: ICMP can survive
        #    a proxy whose TCP outbound is dead. 3m, not 1m: a restarted mihomo
        #    needs time to fetch its subscription before being probed again.
        /tool netwatch add host=$dohIP port=443 interval=3m timeout=5s type=tcp-conn comment="mtvpn:watch-tunnel" down-script=($restart . "tunnel dead\"")
        :put "container: ok"
    } on-error={
        :put "!! container setup FAILED - check: /system package print (container installed?),"
        :put "!! /system device-mode print, /disk print (usb1 formatted ext4?)"
        :log error "fresh-router: container setup failed"
    }
} else={
    :put "container: skipped (device-mode)"
}

# system
/system clock set time-zone-name=$timeZone
/system ntp client set enabled=yes
/system ntp client servers add address=216.239.35.0
/system ntp client servers add address=162.159.200.1
/ip service set ftp disabled=yes
/ip service set telnet disabled=yes
/ip service set www disabled=yes
/ip service set api disabled=yes
/ip service set api-ssl disabled=yes
/tool graphing resource add store-on-disk=yes

# Telegram IPv4 ranges (domain lists don't cover TG's raw-IP clients). The fetch
# rides the tunnel because core.telegram.org is pinned into $vpnList above. The
# tag is NOT "telegram": mtvpn tags its telegram service comment=telegram and the
# commit step below wipes the whole tag before re-adding.
/system scheduler add interval=6h name=Update_Telegram_IPs on-event="/system script run Update_Telegram_CIDR" policy=read,write,policy,test start-time=startup
/system script add dont-require-permissions=no name=Update_Telegram_CIDR \
    policy=read,write,policy,test source=("\
    \n:local url  \"https://core.telegram.org/resources/cidr.txt\"\
    \n:local listName \"" . $vpnList . "\"\
    \n:local tag \"telegram-cidr\"\
    \n:local minEntries 5\
    \n:local content \"\"\
    \n:local newNets [:toarray \"\"]\
    \n:log info \"TG CIDR: start\"\
    \n\
    \n# download straight into memory (no file, no USB writes)\
    \n:do {\
    \n    :local res [/tool fetch url=\$url output=user as-value check-certificate=no]\
    \n    :if ((\$res->\"status\") = \"finished\") do={ :set content (\$res->\"data\") }\
    \n} on-error={\
    \n    :log error \"TG CIDR: download failed\"\
    \n}\
    \n\
    \n# parse: accept IPv4 CIDR lines only\
    \n:local total [:len \$content]\
    \n:local start 0\
    \n:while (\$start < \$total) do={\
    \n    :local end [:find \$content \"\\n\" \$start]\
    \n    :if ([:typeof \$end] = \"nil\") do={ :set end \$total }\
    \n    :local line [:pick \$content \$start \$end]\
    \n    :do {\
    \n        :if ([:len \$line] > 0 && [:pick \$line ([:len \$line] - 1) [:len \$line]] = \"\\r\") do={\
    \n            :set line [:pick \$line 0 ([:len \$line] - 1)]\
    \n        }\
    \n        :while ([:len \$line] > 0 && [:pick \$line 0 1] = \" \") do={\
    \n            :set line [:pick \$line 1 [:len \$line]]\
    \n        }\
    \n        :while ([:len \$line] > 0 && [:pick \$line ([:len \$line] - 1) [:len \$line]] = \" \") do={\
    \n            :set line [:pick \$line 0 ([:len \$line] - 1)]\
    \n        }\
    \n        :local slash [:find \$line \"/\"]\
    \n        :if ([:len \$line] > 0 && [:typeof \$slash] != \"nil\") do={\
    \n            :local ipVal [:toip [:pick \$line 0 \$slash]]\
    \n            :local maskVal [:tonum [:pick \$line (\$slash + 1) [:len \$line]]]\
    \n            :if ([:typeof \$ipVal] = \"ip\" && [:typeof \$maskVal] = \"num\" && \$maskVal >= 0 && \$maskVal <= 32) do={\
    \n                :set newNets (\$newNets , \$line)\
    \n            }\
    \n        }\
    \n    } on-error={}\
    \n    :set start (\$end + 1)\
    \n}\
    \n\
    \n# commit only if the result looks sane - never empty the list on failure\
    \n:local count [:len \$newNets]\
    \n:if (\$count >= \$minEntries) do={\
    \n    /ip firewall address-list remove [find list=\$listName comment=\$tag]\
    \n    :local added 0\
    \n    :foreach net in=\$newNets do={\
    \n        :do {\
    \n            /ip firewall address-list add list=\$listName address=\$net comment=\$tag\
    \n            :set added (\$added + 1)\
    \n        } on-error={\
    \n            :log warning (\"TG CIDR: could not add \" . \$net)\
    \n        }\
    \n    }\
    \n    :log info (\"TG CIDR: updated - \" . \$added . \" of \" . \$count . \" IPv4 subnets active\")\
    \n} else={\
    \n    :log error (\"TG CIDR: only \" . \$count . \" subnets parsed (need >= \" . \$minEntries . \") - list left untouched\")\
    \n}\
    \n:log info \"TG CIDR: finished\"")

/ip neighbor discovery-settings set discover-interface-list=$lanList
/tool mac-server set allowed-interface-list=$lanList
/tool mac-server mac-winbox set allowed-interface-list=$lanList

# IPv4-only selective routing: a dual-stack client would otherwise reach an
# AAAA-capable service direct over the WAN. Takes effect on reboot.
:do { /ipv6 settings set disable-ipv6=yes } on-error={}

:put "fresh-router: import finished"
