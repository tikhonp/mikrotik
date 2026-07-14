# fresh-router.rsc — bootstrap a fresh MikroTik with selective-VPN routing
#
# Replicates the proven hEX S / RB3011 design: domains in address-list
# to_vpn_list are routed through a mihomo container (VLESS), everything else
# goes direct. Fail-open: if the container dies, VPN-listed traffic falls
# back to direct WAN (check-gateway=ping).
#
# USAGE
#   1. Edit the variables in the PARAMETERS section below.
#   2. Complete the PREREQUISITES (manual, once per device).
#   3. Upload this file and run:  /import fresh-router.rsc
#   4. From your workstation, add domain services:
#        ./mtvpn.py -c <router-config>.json add anthropic openai youtube ...
#
# PREREQUISITES (before import)
#   - Clean config:      /system reset-configuration no-defaults=yes skip-backup=yes
#   - Container package: install container-<ver>-<arch>.npk (zip from mikrotik.com), reboot
#   - Enable containers: /system/device-mode/update container=yes
#                        (requires power-button press or reboot to confirm)
#   - USB disk:          plug in, then  /disk format-drive usb1 file-system=ext4
#                        (adjust slot name if not usb1)
#   - SSH key:           /user ssh-keys import public-key-file=<your-key>.pub user=admin
#                        (then disable password-authentication)
#
# AFTER IMPORT
#   - The container image is pulled automatically; netwatch starts it once
#     extracted (within ~1-2 min). Check:  /container print
#   - Verify VPN egress:  /tool fetch url=https://ifconfig.me/ip output=user
#     (ifconfig.me must first be added to the list, e.g. via mtvpn or manually)
#   - Once SSH key login works, harden:  /ip ssh set password-authentication=no

# PARAMETERS
# LAN /24 prefix (no trailing dot). Router gets .1, DHCP hands out .20-.254
:local lanNet "10.230.1"
# WAN port (gets its address via DHCP client)
:local wanIface "ether1"
# mihomo subscription URL (SUB1 env of the container)
:local subUrl "https://f.........rptn.txt"
# container image
:local image "registry-1.docker.io/wiktorbgu/mihomo-mikrotik:latest"
:local timeZone "Europe/Moscow"

# bridges & ports
/interface bridge add name=LAN
/interface bridge add name=container
:foreach e in=[/interface ethernet find where name!=$wanIface] do={
    /interface bridge port add bridge=LAN interface=[/interface ethernet get $e name]
}

# container veth (fixed internal subnet 192.168.89.0/24)
/interface veth add name=vless address=192.168.89.2/24 gateway=192.168.89.1
/interface bridge port add bridge=container interface=vless

# addressing / DHCP
/ip address add address=($lanNet . ".1/24") interface=LAN
/ip address add address=192.168.89.1/24 interface=container
/ip pool add name=dhcp_pool ranges=($lanNet . ".20-" . $lanNet . ".254")
/ip dhcp-server add address-pool=dhcp_pool interface=LAN name=dhcp1 disabled=no
/ip dhcp-server network add address=($lanNet . ".0/24") gateway=($lanNet . ".1") dns-server=($lanNet . ".1")
/ip dhcp-client add interface=$wanIface use-peer-dns=no disabled=no

# DNS: DoH to dns.google (bootstrapped by static A records)
/ip dns static add address=8.8.8.8 name=dns.google type=A
/ip dns static add address=8.8.4.4 name=dns.google type=A
/ip dns set allow-remote-requests=yes use-doh-server=https://dns.google/dns-query verify-doh-cert=yes doh-max-concurrent-queries=300 doh-max-server-connections=100 doh-timeout=10s

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

# firewall filter
/ip firewall filter add action=accept chain=input comment="established, related" connection-state=established,related
/ip firewall filter add action=accept chain=input src-address-list=allowed_to_router
/ip firewall filter add action=accept chain=input protocol=icmp
/ip firewall filter add action=drop chain=input in-interface=$wanIface comment="drop all other WAN input"
/ip firewall filter add action=fasttrack-connection chain=forward comment="FastTrack (skips VPN-marked)" connection-mark=no-mark connection-state=established,related
/ip firewall filter add action=accept chain=forward comment="Established, Related" connection-state=established,related
/ip firewall filter add action=drop chain=forward comment="Drop invalid" connection-state=invalid log=yes log-prefix=invalid
/ip firewall filter add action=drop chain=forward comment="Drop incoming packets that are not NAT`ted" connection-nat-state=!dstnat connection-state=new in-interface=$wanIface log=yes log-prefix=!NAT
/ip firewall filter add action=jump chain=forward comment="jump to ICMP filters" jump-target=icmp protocol=icmp
/ip firewall filter add action=drop chain=forward comment="Drop incoming from internet which is not public IP" in-interface=$wanIface log=yes log-prefix=!public src-address-list=not_in_internet
/ip firewall filter add action=drop chain=forward comment="Drop packets from LAN that do not have LAN IP" in-interface=LAN log=yes log-prefix=LAN_!LAN src-address-list=!lan_nets
/ip firewall filter add action=accept chain=icmp comment="echo reply" icmp-options=0:0 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="net unreachable" icmp-options=3:0 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="host unreachable" icmp-options=3:1 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="host unreachable fragmentation required" icmp-options=3:4 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow echo request" icmp-options=8:0 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow time exceed" icmp-options=11:0 protocol=icmp
/ip firewall filter add action=accept chain=icmp comment="allow parameter bad" icmp-options=12:0 protocol=icmp
/ip firewall filter add action=drop chain=icmp comment="deny all other types"

# NAT
/ip firewall nat add action=masquerade chain=srcnat comment="Masquerade LAN -> WAN" out-interface=$wanIface

# selective-VPN routing infrastructure (mtvpn-compatible comments)
/routing table add name=to_vpn_table fib
/ip firewall mangle add chain=prerouting action=mark-connection connection-mark=no-mark dst-address-list=to_vpn_list in-interface=LAN new-connection-mark=to_vpn_mark passthrough=yes comment="mtvpn:conn-lan"
/ip firewall mangle add chain=output action=mark-connection connection-mark=no-mark dst-address-list=to_vpn_list new-connection-mark=to_vpn_mark passthrough=yes comment="mtvpn:conn-out"
/ip firewall mangle add chain=prerouting action=mark-routing connection-mark=to_vpn_mark in-interface=LAN new-routing-mark=to_vpn_table passthrough=no comment="mtvpn:route-pre"
/ip firewall mangle add chain=output action=mark-routing connection-mark=to_vpn_mark new-routing-mark=to_vpn_table passthrough=no comment="mtvpn:route-out"
/ip route add dst-address=0.0.0.0/0 gateway=192.168.89.2 routing-table=to_vpn_table check-gateway=ping comment="mtvpn:route"

# DoH via VPN (DPI kills direct DoH to Google; loop-safe: prerouting mark is LAN-only)
/ip firewall address-list add list=to_vpn_list address=8.8.8.8 comment=doh
/ip firewall address-list add list=to_vpn_list address=8.8.4.4 comment=doh

# container
/container config set registry-url=https://registry-1.docker.io tmpdir=usb1/container-tmp layer-dir=usb1/container-tmp/layer
/container envs add key=SUB1 list=mihomo value=$subUrl
/container add remote-image=$image interface=vless envlists=mihomo dns=8.8.8.8,8.8.4.4 root-dir=usb1/container-tmp/docker/mihomo start-on-boot=yes

# container watchdog: restart mihomo if its IP stops answering
/tool netwatch add host=192.168.89.2 interval=1m timeout=2s type=simple down-script="/container stop [find name~\"mihomo\"]; :delay 5s; /container start [find name~\"mihomo\"]; :log warning \"mihomo restarted\""

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
# after confirming SSH key login works, also run:
#   /ip ssh set password-authentication=no

# Telegram IP ranges updater (domain lists don't cover TG's raw-IP clients)
/system scheduler add interval=6h name=Update_Telegram_IPs on-event="/system script run Update_Telegram_CIDR" policy=read,write,policy,test start-time=03:15:00
/system script
add dont-require-permissions=no name=Update_Telegram_CIDR owner=tikhon \
    policy=read,write,policy,test source="\
    \n#  Update_Telegram_CIDR  -  fail-safe rewrite\
    \n#  Refreshes Telegram IPv4 CIDRs in to_vpn_list.\
    \n#  Never empties the list on failure. Always cleans up.\
    \n\
    \n:local url         \"https://core.telegram.org/resources/cidr.txt\"\
    \n:local resolveHost \"core.telegram.org\"\
    \n:local listName    \"to_vpn_list\"\
    \n:local tag         \"telegram\"\
    \n:local altGateway  \"192.168.89.2\"\
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
    \n:log info \"TG CIDR: finished\""
