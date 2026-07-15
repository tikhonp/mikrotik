#!/usr/bin/env python3
"""mtvpn — bootstrap and manage selective-VPN domain routing on MikroTik RouterOS 7.

Design (policy-based routing):
  - domains land in a firewall address-list (default: to_vpn_list) via two mechanisms:
      1. DNS static FWD entries with match-subdomain=yes  -> covers every subdomain
         a LAN client resolves (dynamic list entries, renewed on each query)
      2. firewall address-list hostname entries           -> apex A records,
         continuously re-resolved by the firewall, survive DNS cache flushes
  - mangle marks connections to listed IPs (to_vpn_mark), marks routing (to_vpn_table)
  - to_vpn_table has one default route via the VPN gateway with check-gateway=ping,
    so when the gateway dies traffic fails open to the main table (direct WAN)

Domain sources: v2fly/domain-list-community service names (e.g. "anthropic",
"openai") or any raw URL to a file in the same format.

Requires: python3 (stdlib only) and ssh key auth to the router.
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

V2FLY_BASE = "https://raw.githubusercontent.com/v2fly/domain-list-community/refs/heads/master/data/"

DEFAULTS = {
    "ssh": "",                # full ssh command, e.g. "ssh -J jumphost 10.0.0.1"
    "gateway": "",            # next-hop IP of the VPN gateway (container/tunnel peer)
    "list": "to_vpn_list",
    "table": "to_vpn_table",
    "mark": "to_vpn_mark",
    "lan_list": "LAN",        # interface list whose traffic is subject to VPN routing
    "services": [],
}

SSH_NOISE = re.compile(r"WARNING|post-quantum|store now|upgraded|^\s*$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


def parse_yaml(text):
    """Minimal YAML subset: flat `key: value` scalars plus one-level lists of
    scalars (`- item`). Full-line comments only. Enough for mtvpn configs."""
    data, last = {}, None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if last is None:
                raise SystemExit(f"config: list item without a key: {raw!r}")
            if not isinstance(data[last], list):
                data[last] = []
            data[last].append(_scalar(stripped[2:]))
        else:
            key, sep, val = stripped.partition(":")
            if not sep:
                raise SystemExit(f"config: cannot parse line: {raw!r}")
            last = key.strip()
            data[last] = _scalar(val)
    return data


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    return s


def dump_yaml(cfg):
    lines = []
    for k, v in cfg.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"  - {i}" for i in v]
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"


def load_config(path):
    cfg = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        text = p.read_text()
        try:  # legacy JSON configs still load; saving writes YAML
            loaded = json.loads(text)
        except ValueError:
            loaded = parse_yaml(text)
        cfg.update(loaded)
    if not cfg.get("ssh") and cfg.get("router"):  # legacy router/ssh_opts keys
        cfg["ssh"] = " ".join(["ssh"] + cfg.pop("ssh_opts", []) + [cfg.pop("router")])
    if isinstance(cfg.get("services"), str):  # empty "services:" line parses as ""
        cfg["services"] = [cfg["services"]] if cfg["services"] else []
    return cfg


def save_config(path, cfg):
    Path(path).write_text(dump_yaml({k: cfg[k] for k in DEFAULTS if k in cfg}))


def service_url(name_or_url):
    """Return (service_name, url) for a v2fly service name or a raw URL."""
    if "://" in name_or_url:
        return name_or_url.rstrip("/").rsplit("/", 1)[-1].lower(), name_or_url
    return name_or_url.lower(), V2FLY_BASE + name_or_url.lower()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mtvpn/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def parse_list(url, seen=None):
    """Parse a v2fly-format list. Returns (subdomain_domains, exact_domains, skipped).

    Handles: plain domains (match subdomains), full:, domain:, include: (recursive).
    Skips regexp:/keyword: rules — RouterOS cannot express them.
    """
    if seen is None:
        seen = set()
    if url in seen:
        return set(), set(), []
    seen.add(url)

    sub, full, skipped = set(), set(), []
    base = url.rsplit("/", 1)[0] + "/"

    for raw in fetch(url).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        rule = line.split()[0]  # drop @attrs (e.g. @ads @cn)

        if rule.startswith("include:"):
            s2, f2, sk2 = parse_list(base + rule[8:], seen)
            sub |= s2
            full |= f2
            skipped += sk2
        elif rule.startswith("full:"):
            full.add(rule[5:].lower())
        elif rule.startswith("domain:"):
            sub.add(rule[7:].lower())
        elif rule.startswith(("regexp:", "keyword:")):
            skipped.append(rule)
        else:
            sub.add(rule.lower())

    bad = {d for d in sub | full if not DOMAIN_RE.match(d)}
    if bad:
        skipped += sorted(bad)
        sub -= bad
        full -= bad
    return sub, full, skipped


def rsc_service(svc, sub, full, cfg):
    """RouterOS commands that install/refresh one service's domains, idempotently.

    Entries are tagged comment=<svc>. Untagged duplicates (manual entries for the
    same domain) are adopted: removed first, re-added with the tag.
    """
    L = cfg["list"]
    out = [
        f'/ip dns static remove [find comment="{svc}" address-list="{L}"]',
        f'/ip firewall address-list remove [find list="{L}" comment="{svc}" dynamic=no]',
    ]
    for domain, match_sub in [(d, "yes") for d in sorted(sub)] + [(d, "no") for d in sorted(full)]:
        out += [
            f':do {{/ip dns static remove [find name="{domain}" type=FWD address-list="{L}"]}} on-error={{}}',
            f'/ip dns static add name={domain} type=FWD match-subdomain={match_sub} '
            f'address-list={L} comment="{svc}"',
            f':do {{/ip firewall address-list remove [find list="{L}" address="{domain}" dynamic=no]}} on-error={{}}',
            f':do {{/ip firewall address-list add list={L} address={domain} comment="{svc}"}} on-error={{}}',
        ]
    return out


def rsc_remove_service(svc, cfg):
    L = cfg["list"]
    return [
        f'/ip dns static remove [find comment="{svc}" address-list="{L}"]',
        f'/ip firewall address-list remove [find list="{L}" comment="{svc}" dynamic=no]',
    ]


def rsc_bootstrap(cfg, fix_fasttrack=False):
    """Idempotent infrastructure: routing table, mangle rules, fail-open VPN route."""
    L, T, M, lan, gw = cfg["list"], cfg["table"], cfg["mark"], cfg["lan_list"], cfg["gateway"]
    if not gw:
        raise SystemExit("bootstrap needs --gateway (next-hop IP of the VPN gateway)")

    def once(find_expr, add_cmd):
        return f':if ([:len [{find_expr}]] = 0) do={{{add_cmd}}}'

    out = [
        once(f'/routing table find name="{T}"',
             f'/routing table add name={T} fib'),
        # 1-2: mark new connections to listed IPs. in-interface-list=LAN on the
        # prerouting rule is deliberate: gateway/container-originated traffic must
        # NOT be marked or it loops back into the gateway.
        once(f'/ip firewall mangle find comment="mtvpn:conn-lan"',
             f'/ip firewall mangle add chain=prerouting action=mark-connection '
             f'connection-mark=no-mark dst-address-list={L} in-interface-list={lan} '
             f'new-connection-mark={M} passthrough=yes comment="mtvpn:conn-lan"'),
        once(f'/ip firewall mangle find comment="mtvpn:conn-out"',
             f'/ip firewall mangle add chain=output action=mark-connection '
             f'connection-mark=no-mark dst-address-list={L} '
             f'new-connection-mark={M} passthrough=yes comment="mtvpn:conn-out"'),
        # 3-4: route every packet of a marked connection through the VPN table
        once(f'/ip firewall mangle find comment="mtvpn:route-pre"',
             f'/ip firewall mangle add chain=prerouting action=mark-routing '
             f'connection-mark={M} in-interface-list={lan} '
             f'new-routing-mark={T} passthrough=no comment="mtvpn:route-pre"'),
        once(f'/ip firewall mangle find comment="mtvpn:route-out"',
             f'/ip firewall mangle add chain=output action=mark-routing '
             f'connection-mark={M} new-routing-mark={T} passthrough=no comment="mtvpn:route-out"'),
        # clamp TCP MSS on tunneled flows: VLESS/Reality encapsulation shrinks the
        # path MTU, so unclamped full-size segments stall. Match by connection-mark
        # (rides every packet) to clamp both the SYN and SYN-ACK -> both directions.
        once(f'/ip firewall mangle find comment="mtvpn:mss-clamp"',
             f'/ip firewall mangle add chain=forward action=change-mss '
             f'new-mss=1360 passthrough=yes protocol=tcp tcp-flags=syn '
             f'connection-mark={M} tcp-mss=1361-65535 comment="mtvpn:mss-clamp"'),
        # default route via the gateway; check-gateway=ping -> fail-open to main
        # table (direct WAN) when the gateway is down
        once(f'/ip route find where routing-table={T} comment="mtvpn:route"',
             f'/ip route add dst-address=0.0.0.0/0 gateway={gw} routing-table={T} '
             f'check-gateway=ping comment="mtvpn:route"'),
    ]
    if fix_fasttrack:
        # fasttrack skips mangle for established connections; exclude marked ones
        out.append(
            ':foreach i in=[/ip firewall filter find action=fasttrack-connection] '
            'do={:do {:if ([:len [:tostr [/ip firewall filter get $i connection-mark]]] = 0) '
            f'do={{/ip firewall filter set $i connection-mark=!{cfg["mark"]}}}}} on-error={{}}}}'
        )
    return out


def ssh_base(cfg):
    cmd = shlex.split(cfg["ssh"])
    if not cmd:
        raise SystemExit('no ssh target: set "ssh:" in the config or pass -r "ssh <host>"')
    if cmd[0] != "ssh":
        cmd.insert(0, "ssh")
    return [cmd[0], "-o", "BatchMode=yes"] + cmd[1:]


def target(cfg):
    """Short display name for the router: last token of the ssh command."""
    return cfg["ssh"].split()[-1] if cfg["ssh"] else "(dry-run)"


def push(cfg, script, dry_run=False):
    text = "\n".join(script) + "\n"
    if dry_run:
        print(text, end="")
        return
    r = subprocess.run(
        ssh_base(cfg),
        input=text, capture_output=True, text=True, timeout=300,
    )
    noise_free = [l for l in (r.stdout + r.stderr).splitlines() if not SSH_NOISE.search(l)]
    for l in noise_free:
        print("  router:", l)
    if r.returncode != 0:
        raise SystemExit(f"ssh to {target(cfg)} failed (exit {r.returncode})")


def query(cfg, command):
    r = subprocess.run(
        ssh_base(cfg) + [command],
        capture_output=True, text=True, timeout=120,
    )
    return [l for l in r.stdout.splitlines() if not SSH_NOISE.search(l)]


def cmd_bootstrap(cfg, args):
    script = rsc_bootstrap(cfg, fix_fasttrack=args.fix_fasttrack)
    for name in cfg["services"]:
        svc, url = service_url(name)
        sub, full, skipped = parse_list(url)
        report_parse(svc, sub, full, skipped)
        script += rsc_service(svc, sub, full, cfg)
    push(cfg, script, args.dry_run)
    if not args.dry_run:
        print(f"bootstrapped {target(cfg)}: infra + {len(cfg['services'])} service(s)")
        print("NOTE: LAN clients must use the router as their only DNS server "
              "(DHCP dns-server, /ip dns allow-remote-requests=yes), or the "
              "FWD-based subdomain coverage will not populate the list.")


def cmd_add(cfg, args, config_path):
    for name in args.services:
        svc, url = service_url(name)
        sub, full, skipped = parse_list(url)
        report_parse(svc, sub, full, skipped)
        push(cfg, rsc_service(svc, sub, full, cfg), args.dry_run)
        if not args.dry_run:
            print(f"added '{svc}' ({len(sub) + len(full)} domains) on {target(cfg)}")
        if not args.dry_run and Path(config_path).exists() and name not in cfg["services"]:
            cfg["services"].append(name)
            save_config(config_path, cfg)


def cmd_update(cfg, args, config_path):
    args.services = args.services or cfg["services"]
    if not args.services:
        raise SystemExit("nothing to update: no services given and none in config")
    cmd_add(cfg, args, config_path)


def cmd_remove(cfg, args, config_path):
    for name in args.services:
        svc, _ = service_url(name)
        push(cfg, rsc_remove_service(svc, cfg), args.dry_run)
        if not args.dry_run:
            print(f"removed '{svc}' from {target(cfg)}")
        if not args.dry_run and Path(config_path).exists():
            cfg["services"] = [s for s in cfg["services"] if service_url(s)[0] != svc]
            save_config(config_path, cfg)


def cmd_list(cfg, args):
    lines = query(
        cfg,
        ':foreach i in=[/ip dns static find where address-list="%s"] '
        'do={:put ([:tostr [/ip dns static get $i comment]] . "|" '
        '. [/ip dns static get $i name])}' % cfg["list"],
    )
    services = {}
    for l in lines:
        if "|" in l:
            svc, name = l.split("|", 1)
            services.setdefault(svc or "(untagged)", []).append(name)
    for svc in sorted(services):
        print(f"{svc}: {len(services[svc])} domains")
        if args.verbose:
            for n in sorted(services[svc]):
                print(f"   {n}")


def cmd_render(cfg, args):
    for name in args.services:
        svc, url = service_url(name)
        sub, full, skipped = parse_list(url)
        report_parse(svc, sub, full, skipped)
        print("\n".join(rsc_service(svc, sub, full, cfg)))


def report_parse(svc, sub, full, skipped):
    print(f"# {svc}: {len(sub)} subdomain-match + {len(full)} exact domains", file=sys.stderr)
    for s in skipped:
        print(f"#   skipped (unsupported on RouterOS): {s}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="Selective-VPN domain routing on MikroTik from v2fly domain lists")
    ap.add_argument("-c", "--config", default="mtvpn.yaml", help="config file (default mtvpn.yaml)")
    ap.add_argument("-r", "--router", metavar="SSH_CMD",
                    help='ssh command for the router, e.g. "ssh -J jumphost 10.0.0.1"')
    ap.add_argument("-n", "--dry-run", action="store_true", help="print RouterOS commands, don't push")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("bootstrap", help="install routing infra + all services from config")
    p.add_argument("--gateway", help="next-hop IP of the VPN gateway")
    p.add_argument("--fix-fasttrack", action="store_true",
                   help="exclude marked connections from existing fasttrack rules")

    for c, h in [("add", "fetch service list(s) and install (or refresh) them"),
                 ("update", "re-fetch and refresh services (default: all from config)"),
                 ("remove", "remove service(s) from the router"),
                 ("render", "print RouterOS commands for service(s) to stdout")]:
        p = sp.add_parser(c, help=h)
        p.add_argument("services", nargs="*" if c == "update" else "+",
                       help="v2fly service name or raw URL")

    p = sp.add_parser("list", help="show services installed on the router")
    p.add_argument("-v", "--verbose", action="store_true", help="also list domains")

    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.router:
        cfg["ssh"] = args.router
    if getattr(args, "gateway", None):
        cfg["gateway"] = args.gateway
    if args.cmd != "render" and not cfg["ssh"] and not args.dry_run:
        raise SystemExit('no router: use -r "ssh <host>" or set "ssh:" in the config file')

    if args.cmd == "bootstrap":
        cmd_bootstrap(cfg, args)
    elif args.cmd == "add":
        cmd_add(cfg, args, args.config)
    elif args.cmd == "update":
        cmd_update(cfg, args, args.config)
    elif args.cmd == "remove":
        cmd_remove(cfg, args, args.config)
    elif args.cmd == "list":
        cmd_list(cfg, args)
    elif args.cmd == "render":
        cmd_render(cfg, args)


if __name__ == "__main__":
    main()
