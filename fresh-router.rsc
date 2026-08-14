# fresh-router.rsc — bootstrap a fresh MikroTik with selective-VPN routing
#
# USAGE
#   1. Edit the variables in the PARAMETERS section below.
#   2. Complete the PREREQUISITES (manual, once per device).
#   3. Upload this file and run:  /import fresh-router.rsc
#   4. From your workstation, add domain services:
#        ./mtvpn.py -c <router-config>.yaml add anthropic openai youtube ...
#
# PREREQUISITES (before import)
#   - Clean config:      /system reset-configuration no-defaults=yes skip-backup=yes
#   - Device mode:       /system/device-mode/update mode=advanced container=yes scheduler=yes fetch=yes
#                        Verify with: /system device-mode print
#   - USB disk:          /disk format usb1 file-system=ext4
#   - SSH key:           /user ssh-keys import public-key-file=key.pub user=...
#                        /ip ssh set password-authentication=no
#   - Disable admin:     /user disable admin
#

# PARAMETERS
# LAN /24 prefix (no trailing dot). Router gets .1, DHCP hands out .20-.254
:local lanNet "10.220.1"
# WAN port (gets its address via DHCP client)
:local wanIface "ether1"
# mihomo subscription URL (SUB1 env of the container)
:local subUrl "https://files.tikhonnnnn.com/share/g53vy3i9waby/sbscrptn.txt"
# container image
:local image "registry-1.docker.io/wiktorbgu/mihomo-mikrotik:latest"
:local timeZone "Europe/Moscow"

# interface names
:local lanIface "LAN"
:local containerIface "container"
:local vethName "vless"

# interface *lists*. Every firewall/mangle rule below matches on these rather
# than on interface names, so a second LAN segment or WAN uplink is a one-line
# membership change. Named distinctly from the bridges on purpose: RouterOS does
# not document whether a list may share a name with an interface, and a rejected
# /interface list add would abort the whole import.
:local lanList "LANiface"
:local wanList "WANiface"

# selective-VPN routing names — MUST match this router's mtvpn <config>.yaml
# (list: / table: / mark: / lan_list:) or mtvpn add/update/remove won't line up
# with the rules created below. In particular set
#     lan_list: LANiface
# in that config: mtvpn's built-in default is "LAN", which on this router is the
# *bridge*, not a list, so `mtvpn bootstrap` would emit in-interface-list=LAN
# and fail to add its prerouting rules.
:local vpnList "to_vpn_list"
:local vpnTable "to_vpn_table"
:local vpnMark "to_vpn_mark"

# container internal /24: router side = .1, mihomo veth = .2 = the VPN gateway
:local containerNet "192.168.89"
:local vpnGateway ($containerNet . ".2")

# PRECHECK device-mode
:foreach need in={"container";"scheduler";"fetch"} do={
    :if ([/system device-mode get $need] != true) do={
        :error ("device-mode blocks '" . $need . "' (mode=" . [/system device-mode get mode] . \
            "). Run: /system/device-mode/update mode=advanced container=yes scheduler=yes fetch=yes " . \
            "then confirm with the reset button, and re-run this import.")
    }
}

# bridges & ports
/interface bridge add name=$lanIface
/interface bridge add name=$containerIface
:foreach e in=[/interface ethernet find where name!=$wanIface] do={
    /interface bridge port add bridge=$lanIface interface=[/interface ethernet get $e name]
}

# Pin the bridge MAC to the first LAN port. With auto-mac=yes (the default) the
# bridge borrows the MAC of the lowest-numbered *running* port, so it changes
# whenever ports go up or down and LAN clients have to re-ARP their gateway.
:local lanPorts [/interface ethernet find where name!=$wanIface]
:if ([:len $lanPorts] > 0) do={
    /interface bridge set [find name=$lanIface] auto-mac=no admin-mac=[/interface ethernet get [:pick $lanPorts 0] mac-address]
}

# interface lists. The *bridge* is the member, not its ports: for routed traffic
# the IP firewall sees the bridge as in-interface, so listing the ports instead
# would match nothing.
/interface list add name=$lanList
/interface list add name=$wanList
/interface list member add list=$lanList interface=$lanIface
/interface list member add list=$wanList interface=$wanIface
# The container bridge is deliberately in neither list: mihomo then cannot reach
# router services (the input chain drops anything not from LAN), and its egress
# is never mistaken for LAN traffic by the VPN mangle rules below.

# container veth (internal subnet $containerNet.0/24; veth .2 = VPN gateway)
/interface veth add name=$vethName address=($containerNet . ".2/24") gateway=($containerNet . ".1")
/interface bridge port add bridge=$containerIface interface=$vethName

# addressing / DHCP
/ip address add address=($lanNet . ".1/24") interface=$lanIface
/ip address add address=($containerNet . ".1/24") interface=$containerIface
/ip pool add name=dhcp_pool ranges=($lanNet . ".20-" . $lanNet . ".254")
/ip dhcp-server add address-pool=dhcp_pool interface=$lanIface name=dhcp1 disabled=no
/ip dhcp-server network add address=($lanNet . ".0/24") gateway=($lanNet . ".1") dns-server=($lanNet . ".1")
/ip dhcp-client add interface=$wanIface use-peer-dns=no disabled=no

# DNS: DoH to dns.google (bootstrapped by static A records)
/ip dns static add address=8.8.8.8 name=dns.google type=A
/ip dns static add address=8.8.4.4 name=dns.google type=A
# plain A record, no address-list= -> mtvpn never touches it (it only ever
# finds/removes /ip dns static entries that carry address-list=)
/ip dns static add address=($lanNet . ".1") name=router.lan type=A
/ip dns set allow-remote-requests=yes use-doh-server=https://dns.google/dns-query verify-doh-cert=yes doh-max-concurrent-queries=100 doh-max-server-connections=20 doh-timeout=10s cache-size=16384KiB

# firewall address lists
/ip firewall address-list add list=lan_nets address=($lanNet . ".0/24")
/ip firewall address-list add list=allowed_to_router address=($lanNet . ".0/24")
/ip firewall address-list add address=0.0.0.0/8 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=172.16.0.0/12 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=192.168.0.0/16 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=10.0.0.0/8 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=169.254.0.0/16 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=127.0.0.0/8 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=224.0.0.0/4 comment=Multicast list=not_in_internet
/ip firewall address-list add address=198.18.0.0/15 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=192.0.0.0/24 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=192.0.2.0/24 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=198.51.100.0/24 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=203.0.113.0/24 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=100.64.0.0/10 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=240.0.0.0/4 comment=RFC6890 list=not_in_internet
/ip firewall address-list add address=192.88.99.0/24 comment="6to4 relay Anycast [RFC 3068]" list=not_in_internet

# No /ip firewall raw rules on purpose. MikroTik's "Building Advanced Firewall"
# puts a bad_tcp flag-validation chain here, but raw sits ahead of connection
# tracking in the packet flow, so every rule in it is evaluated on every packet
# — including FastTracked ones, which bypass the filter chain but not raw. That
# put ~9 evaluations per TCP packet on the hot path to block malformed-flag
# scans that the input default-deny and the forward !dstnat rule already drop.
# To measure before re-adding: /ip firewall raw print stats during a large
# download — if a raw rule's counter climbs at line rate, raw is on the fast path.

# firewall filter
# input is default-deny: the filter policy is accept, so without the final drop
# anything that is not explicitly matched reaches the router — the container
# bridge today, any tunnel or VLAN added later. A new WireGuard/Tailscale
# interface must join $lanList (or get its own accept) or its input is dropped.
/ip firewall filter add action=accept chain=input comment="established, related, untracked" connection-state=established,related,untracked
/ip firewall filter add action=drop chain=input comment="drop invalid" connection-state=invalid
# in-interface-list= as well as src-address-list=: without it a WAN packet
# spoofing a LAN source address would be accepted by the router.
/ip firewall filter add action=accept chain=input comment="trusted LAN sources" in-interface-list=$lanList src-address-list=allowed_to_router
# No DHCP accept rule is needed here even though a DISCOVER has src 0.0.0.0 and
# therefore misses allowed_to_router above: RouterOS handles DHCP before the
# filter chain. Measured on a live hEX S — an explicit accept rule sat at 0
# packets while 13 clients renewed 30-minute leases, so DHCP never reaches this
# chain at all. (The stock MikroTik config is the other half of the proof: it
# drops the WAN interface outright yet its DHCP client still gets a lease.)
# rate-limited: this rule (not the icmp chain below) is what answers pings to the
# router itself. LAN pings never reach it — they are already accepted by the rule
# above — so the limit only throttles ICMP arriving from elsewhere, and
# over-limit packets fall through to the default deny.
/ip firewall filter add action=accept chain=input comment="allow ping to the router" limit=5,10:packet protocol=icmp
/ip firewall filter add action=drop chain=input comment="default deny: anything not accepted above"
/ip firewall filter add action=fasttrack-connection chain=forward comment="FastTrack (skips VPN-marked)" connection-mark=no-mark connection-state=established,related
/ip firewall filter add action=accept chain=forward comment="Established, Related, Untracked" connection-state=established,related,untracked
# These four drops deliberately do NOT log. "invalid" and "!NAT" fire constantly
# from background internet scanning, and a log write per dropped packet turns a
# scan into a self-inflicted CPU load — the more junk arrives, the more work the
# router does. Add log=yes log-prefix=<x> back temporarily when debugging.
/ip firewall filter add action=drop chain=forward comment="Drop invalid" connection-state=invalid
/ip firewall filter add action=drop chain=forward comment="Drop incoming packets that are not NAT`ted" connection-nat-state=!dstnat connection-state=new in-interface-list=$wanList
/ip firewall filter add action=jump chain=forward comment="jump to ICMP filters" jump-target=icmp protocol=icmp
/ip firewall filter add action=drop chain=forward comment="Drop incoming from internet which is not public IP" in-interface-list=$wanList src-address-list=not_in_internet
/ip firewall filter add action=drop chain=forward comment="Drop packets from LAN that do not have LAN IP" in-interface-list=$lanList src-address-list=!lan_nets
# forwarded ICMP only (reached via the jump above). Echo request/reply are
# rate-limited; over-limit packets fall through to the final drop in this chain.
/ip firewall filter add action=accept chain=icmp comment="echo reply" icmp-options=0:0 limit=5,10:packet protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="net unreachable" icmp-options=3:0 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="host unreachable" icmp-options=3:1 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="host unreachable fragmentation required" icmp-options=3:4 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow echo request" icmp-options=8:0 limit=5,10:packet protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow time exceed" icmp-options=11:0 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow parameter bad" icmp-options=12:0 protocol=icmp
/ip firewall filter add action=drop chain=icmp comment="deny all other types"

# NAT
/ip firewall nat add action=masquerade chain=srcnat comment="Masquerade LAN -> WAN" out-interface-list=$wanList

# selective-VPN routing infrastructure (mtvpn-compatible comments)
/routing table add name=$vpnTable fib
/ip firewall mangle add chain=prerouting action=mark-connection connection-mark=no-mark dst-address-list=$vpnList in-interface-list=$lanList new-connection-mark=$vpnMark passthrough=yes comment="mtvpn:conn-lan"
/ip firewall mangle add chain=output action=mark-connection connection-mark=no-mark dst-address-list=$vpnList new-connection-mark=$vpnMark passthrough=yes comment="mtvpn:conn-out"
/ip firewall mangle add chain=prerouting action=mark-routing connection-mark=$vpnMark in-interface-list=$lanList new-routing-mark=$vpnTable passthrough=no comment="mtvpn:route-pre"
/ip firewall mangle add chain=output action=mark-routing connection-mark=$vpnMark new-routing-mark=$vpnTable passthrough=no comment="mtvpn:route-out"
# clamp TCP MSS on tunneled flows: VLESS/Reality encapsulation shrinks the path
# MTU, so unclamped full-size segments stall ("some sites hang over VPN"). Match
# by connection-mark (rides every packet) so both the SYN and SYN-ACK are clamped
# -> both directions covered, no dependence on the container interface name.
/ip firewall mangle add chain=forward action=change-mss new-mss=1360 passthrough=yes protocol=tcp tcp-flags=syn connection-mark=$vpnMark tcp-mss=1361-65535 comment="mtvpn:mss-clamp"
/ip route add dst-address=0.0.0.0/0 gateway=$vpnGateway routing-table=$vpnTable check-gateway=ping comment="mtvpn:route"

# container
/container config set registry-url=https://registry-1.docker.io tmpdir=usb1/container-tmp layer-dir=usb1/container-tmp/layer
/container envs add key=SUB1 list=mihomo value=$subUrl
/container add remote-image=$image interface=$vethName envlists=mihomo dns=8.8.8.8,8.8.4.4 root-dir=usb1/container-tmp/docker/mihomo start-on-boot=yes

# container watchdog: restart mihomo if its IP stops answering
/tool netwatch add host=$vpnGateway interval=1m timeout=2s type=simple down-script="/container stop [find name~\"mihomo\"]; :delay 5s; /container start [find name~\"mihomo\"]; :log warning \"mihomo restarted\""

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

# Telegram IP ranges updater (domain lists don't cover TG's raw-IP clients)
/system scheduler add interval=6h name=Update_Telegram_IPs on-event="/system script run Update_Telegram_CIDR" policy=read,write,policy,test start-time=startup
/system script
add dont-require-permissions=no name=Update_Telegram_CIDR owner=tikhon \
    policy=read,write,policy,test source=("\
    \n#  Update_Telegram_CIDR  -  fail-safe rewrite\
    \n#  Refreshes Telegram IPv4 CIDRs in to_vpn_list.\
    \n#  Never empties the list on failure. Always cleans up.\
    \n\
    \n:local url         \"https://core.telegram.org/resources/cidr.txt\"\
    \n:local resolveHost \"core.telegram.org\"\
    \n:local listName    \"" . $vpnList . "\"\
    \n# NOT \"telegram\": mtvpn tags its telegram service entries comment=telegram,\
    \n# and step 4 below removes the whole tag before re-adding. A shared tag makes\
    \n# the two wipe each other's entries.\
    \n:local tag         \"telegram-cidr\"\
    \n:local altGateway  \"" . $vpnGateway . "\"\
    \n:local routeTag    \"TEMP_TG_FETCH\"\
    \n:local minEntries  5\
    \n\
    \n:local content \"\"\
    \n:local newNets [:toarray \"\"]\
    \n:local tgIP \"\"\
    \n\
    \n:log info \"TG CIDR: start\"\
    \n\
    \n# 0. clear leftovers from any previous crashed run\
    \n/ip route remove [find comment=\$routeTag]\
    \n\
    \n# 1. temporary route so the fetch itself goes via the tunnel\
    \n:do {\
    \n    :set tgIP [:resolve \$resolveHost]\
    \n} on-error={\
    \n    :log warning \"TG CIDR: resolve failed\"\
    \n}\
    \n:if ([:len \$tgIP] > 0) do={\
    \n    :do {\
    \n        /ip route add dst-address=\"\$tgIP/32\" gateway=\$altGateway com\
    ment=\$routeTag\
    \n        :delay 2s\
    \n    } on-error={\
    \n        :log warning \"TG CIDR: temp route not added\"\
    \n    }\
    \n}\
    \n\
    \n# 2. download straight into memory (no file, no USB writes)\
    \n:do {\
    \n    :local res [/tool fetch url=\$url output=user as-value check-certifi\
    cate=no]\
    \n    :if ((\$res->\"status\") = \"finished\") do={\
    \n        :set content (\$res->\"data\")\
    \n    }\
    \n} on-error={\
    \n    :log error \"TG CIDR: download failed\"\
    \n}\
    \n\
    \n# 3. parse: accept IPv4 CIDR lines only\
    \n:local total [:len \$content]\
    \n:local start 0\
    \n:while (\$start < \$total) do={\
    \n    :local end [:find \$content \"\\n\" \$start]\
    \n    :if ([:typeof \$end] = \"nil\") do={ :set end \$total }\
    \n\
    \n    :local line [:pick \$content \$start \$end]\
    \n\
    \n    :do {\
    \n        :if ([:len \$line] > 0) do={\
    \n            :if ([:pick \$line ([:len \$line] - 1) [:len \$line]] = \"\\\
    r\") do={\
    \n                :set line [:pick \$line 0 ([:len \$line] - 1)]\
    \n            }\
    \n        }\
    \n        :while ([:len \$line] > 0 && [:pick \$line 0 1] = \" \") do={\
    \n            :set line [:pick \$line 1 [:len \$line]]\
    \n        }\
    \n        :while ([:len \$line] > 0 && [:pick \$line ([:len \$line] - 1) [\
    :len \$line]] = \" \") do={\
    \n            :set line [:pick \$line 0 ([:len \$line] - 1)]\
    \n        }\
    \n\
    \n        :local slash [:find \$line \"/\"]\
    \n        :if ([:len \$line] > 0 && [:typeof \$slash] != \"nil\") do={\
    \n            :local ipVal   [:toip [:pick \$line 0 \$slash]]\
    \n            :local maskVal [:tonum [:pick \$line (\$slash + 1) [:len \$l\
    ine]]]\
    \n            :if ([:typeof \$ipVal] = \"ip\" && [:typeof \$maskVal] = \"n\
    um\" && \$maskVal >= 0 && \$maskVal <= 32) do={\
    \n                :set newNets (\$newNets , \$line)\
    \n            }\
    \n        }\
    \n    } on-error={}\
    \n\
    \n    :set start (\$end + 1)\
    \n}\
    \n\
    \n# 4. commit only if the result looks sane\
    \n:local count [:len \$newNets]\
    \n:if (\$count >= \$minEntries) do={\
    \n    /ip firewall address-list remove [find list=\$listName comment=\$tag\
    ]\
    \n    :local added 0\
    \n    :foreach net in=\$newNets do={\
    \n        :do {\
    \n            /ip firewall address-list add list=\$listName address=\$net \
    comment=\$tag\
    \n            :set added (\$added + 1)\
    \n        } on-error={\
    \n            :log warning (\"TG CIDR: could not add \" . \$net)\
    \n        }\
    \n    }\
    \n    :log info (\"TG CIDR: updated - \" . \$added . \" of \" . \$count . \
    \" IPv4 subnets active\")\
    \n} else={\
    \n    :log error (\"TG CIDR: only \" . \$count . \" subnets parsed (need >\
    = \" . \$minEntries . \") - existing list left untouched\")\
    \n}\
    \n\
    \n# 5. cleanup - always reached\
    \n/ip route remove [find comment=\$routeTag]\
    \n:log info \"TG CIDR: finished\"")

/ip neighbor discovery-settings set discover-interface-list=$lanList
/tool mac-server set allowed-interface-list=$lanList
/tool mac-server mac-winbox set allowed-interface-list=$lanList

:do { /ipv6 settings set disable-ipv6=yes } on-error={}
