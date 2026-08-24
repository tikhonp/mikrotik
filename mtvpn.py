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

Domain sources, named explicitly per service and never inferred:
  - iplist:<selector>   iplist.opencck.org, e.g. iplist:youtube.com (a site) or
                        iplist:apple (a group). iplist:<portal>:<selector> pins
                        the portal.
  - v2fly:<name>        v2fly/domain-list-community, e.g. v2fly:anthropic. A bare
                        name means this — what it meant before iplist existed.
  - a raw URL to a file in either format — including a plain one-domain-per-line
    list of your own, `#` comments and all. `<tag>=<url>` names it; otherwise the
    tag comes from the URL. `update --urls-only` refreshes just these.
A selector that one source doesn't carry is an error, not a silent switch to the
other; `search` lists what both have.

The set of services itself can also live on a server: a hosted *service list* is
one selector per line, referenced by the config's `service_lists:` or by
--from-list, and `update` then applies whatever it says today (--prune to also
drop what it stopped saying).

Requires: python3 (stdlib only) and ssh key auth to the router.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

V2FLY_BASE = "https://raw.githubusercontent.com/v2fly/domain-list-community/refs/heads/master/data/"
V2FLY_TREE_API = "https://api.github.com/repos/v2fly/domain-list-community/git/trees/"

# iplist.opencck.org portals, probed in this order. Their catalogs are near-disjoint
# (only ui.com and anydesk.com appear on two), so the order rarely decides anything;
# iplist:<portal>:<selector> pins it for the cases where it does.
IPLIST_PORTALS = [
    ("main", "https://iplist.opencck.org/"),
    ("beta", "https://beta.iplist.opencck.org/"),
    ("russia", "https://russia.iplist.opencck.org/"),
]
SOURCES = ("iplist", "v2fly")

# `<tag>=<url>`: `=` separates only when a bare tag precedes it and a URL scheme
# follows, so a plain URL carrying a query string is never split.
NAMED_URL_RE = re.compile(r"^([a-z0-9][a-z0-9._-]*)=(?=[a-z][a-z0-9+.-]*://)", re.I)
# Extensions dropped when deriving a tag from a URL: a custom list is usually
# served as a file, and "domains.txt" makes a poor router comment.
TAG_EXTENSIONS = (".txt", ".list", ".lst", ".dat", ".conf", ".md")

DEFAULTS = {
    "ssh": "",                # full ssh command, e.g. "ssh -J jumphost 10.0.0.1"
    "scp": "",                # optional scp template override with {local}/{remote}
                              # placeholders; derived from ssh when empty
    "gateway": "",            # next-hop IP of the VPN gateway (container/tunnel peer)
    "list": "to_vpn_list",
    "table": "to_vpn_table",
    "mark": "to_vpn_mark",
    "lan_list": "LAN",        # interface list whose traffic is subject to VPN routing
    "service_lists": [],      # URLs/paths of hosted service lists (see read_service_list)
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
        cfg.update(parse_yaml(p.read_text()))
    if not cfg.get("ssh") and cfg.get("router"):  # legacy router/ssh_opts keys
        cfg["ssh"] = " ".join(["ssh"] + cfg.pop("ssh_opts", []) + [cfg.pop("router")])
    for key in ("services", "service_lists"):  # an empty "key:" line parses as ""
        if isinstance(cfg.get(key), str):
            cfg[key] = [cfg[key]] if cfg[key] else []
    return cfg


def save_config(path, cfg):
    Path(path).write_text(dump_yaml({k: cfg[k] for k in DEFAULTS if k in cfg}))


def split_named_url(name):
    """Split the optional `<tag>=<url>` form. Returns (tag or None, rest).

    A custom domain list has no upstream name to borrow, so its tag would otherwise
    be whatever the URL's last path segment happens to be — which two lists on
    different hosts can easily share, silently merging them under one comment.
    """
    m = NAMED_URL_RE.match(name)
    return (m.group(1).lower(), name[m.end():]) if m else (None, name)


def parse_selector(name):
    """Split a config entry into (source, portal, selector).

    source is "iplist", "v2fly", "url", or None for a bare name — which every
    command that must pick one treats as v2fly, the source bare names meant before
    iplist existed. portal is set only by the iplist:<portal>:<selector> form.
    For a URL the selector is the URL itself, with any `<tag>=` prefix stripped.
    """
    name = split_named_url(name.strip())[1]
    if "://" in name:
        return "url", None, name
    src, sep, rest = name.partition(":")
    src, rest = src.strip().lower(), rest.strip().lower()
    if not sep or src not in SOURCES:
        return None, None, name.lower()
    if src == "v2fly":
        return "v2fly", None, rest
    portal, sep2, rest2 = rest.partition(":")
    if not sep2:
        return "iplist", None, rest
    # Three parts can only be iplist:<portal>:<selector> — no site or group name
    # contains a colon — so an unrecognised middle is a typo, not a selector.
    if portal not in {p for p, _ in IPLIST_PORTALS}:
        raise SystemExit(f"unknown iplist portal {portal!r}: expected one of "
                         f"{', '.join(p for p, _ in IPLIST_PORTALS)}")
    return "iplist", portal, rest2.strip()


def service_tag(name):
    """Router comment tag for a config entry: its selector, minus the source prefix.

    Network-free by contract — `remove` and the services bookkeeping in add/remove
    must not need a fetch to know which entries a name owns. Dropping the prefix is
    also what keeps v2fly:youtube pointed at the comment=youtube entries an older
    mtvpn.yaml already installed.
    """
    src, _, sel = parse_selector(name)
    if src != "url":
        return sel
    tag, _ = split_named_url(name.strip())
    if tag:  # <tag>=<url> says outright what the entry is called
        return tag
    q = urllib.parse.parse_qs(urllib.parse.urlparse(sel).query)
    for kind in ("site", "group"):  # an iplist URL carries its selector in the query
        if q.get(kind):
            return q[kind][0].lower()
    parts = urllib.parse.urlparse(sel)
    # Never fall through to "": the empty tag is what untagged/adopted entries carry,
    # so an entry named "" would own every one of them.
    leaf = (parts.path.rstrip("/").rsplit("/", 1)[-1] or parts.netloc).lower()
    for ext in TAG_EXTENSIONS:
        if leaf.endswith(ext) and len(leaf) > len(ext):
            return leaf[: -len(ext)]
    return leaf


def fetch(url, missing_ok=False):
    req = urllib.request.Request(url, headers={"User-Agent": "mtvpn/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        if missing_ok and e.code == 404:  # v2fly answers a missing list with 404
            return None
        raise


_SERVICE_LISTS: dict = {}


def read_service_list(src):
    """Read a hosted list of *service selectors* (not domains): one per line.

    The point is keeping the set of tunneled services on a server instead of in
    every router's mtvpn.yaml — the file holds exactly what `services:` would
    hold, so a `services:` block can be pasted in verbatim (a leading "- " is
    stripped). `#` comments are honoured, but only at line start or after
    whitespace, so a raw-URL entry keeps its query/fragment.

    `src` is a URL or a local path. Cached per run: bootstrap/add both consult
    the same lists and a hosted list must not change under us mid-command.
    """
    if src in _SERVICE_LISTS:
        return _SERVICE_LISTS[src]
    try:
        text = fetch(src) if "://" in src else Path(src).expanduser().read_text()
    except OSError as e:  # HTTPError/URLError are OSErrors too
        raise SystemExit(f"service list {src}: {e}")
    out = []
    for raw in text.splitlines():
        line = re.split(r"\s#", raw.strip(), maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):  # a pasted YAML services: block
            line = line[2:].strip()
        if not line or line.endswith(":"):  # e.g. the "services:" key itself
            continue
        if any(c.isspace() for c in line):
            raise SystemExit(f"service list {src}: not a service selector: {line!r}")
        out.append(line)
    if not out:
        raise SystemExit(f"service list {src}: no services in it")
    _SERVICE_LISTS[src] = out
    return out


def expand_services(cfg, extra_lists=()):
    """The effective service set: cfg["services"] plus every hosted list's entries.

    Deduped by service_tag, so a service named in both the config and a list is
    installed once and the config's spelling wins — that spelling is the one
    `remove` and the config bookkeeping already match on.
    """
    names, seen = [], set()
    for name in (list(cfg["services"])
                 + [n for src in list(cfg.get("service_lists") or []) + list(extra_lists)
                    for n in read_service_list(src)]):
        tag = service_tag(name)
        if tag not in seen:
            seen.add(tag)
            names.append(name)
    return names


def iplist_url(base, kind, selector):
    """Query URL for one iplist selector; `kind` is "site" or "group".

    wildcard=1 is not optional: wildcard=0 returns every hostname the service has
    ever been seen to use (15k+ for youtube), while the wildcard set is the apex
    list whose meaning matches RouterOS match-subdomain=yes exactly.
    """
    return base + "?" + urllib.parse.urlencode(
        {"format": "text", "data": "domains", "wildcard": "1", kind: selector})


def iplist_find(selector, portal=None):
    """Locate an iplist selector. Returns (source_label, url, text) or None.

    A selector is either a site (youtube.com, apple@icloud.com, copilot) or a group
    (apple, ai, youtube). The two never collide, but the shape doesn't tell them
    apart either — the main portal has dotless sites — so both are probed.

    A portal that doesn't carry the selector answers 200 with an empty body rather
    than 404, so emptiness is the only "not here" signal there is. Which also means
    an unreachable portal must not read as a miss: those are reported separately.
    """
    for name, base in [(p, b) for p, b in IPLIST_PORTALS if portal in (None, p)]:
        for kind in ("site", "group"):
            url = iplist_url(base, kind, selector)
            try:
                text = fetch(url)
            except OSError as e:  # URLError/timeouts both land here
                print(f"# iplist portal {name} unreachable: {e}", file=sys.stderr)
                break  # don't retry the other kind against a dead portal
            if text.strip():
                return f"iplist {name} {kind}", url, text
    return None


def resolve_service(name):
    """Map a config entry to (tag, url, source_label, text).

    `text` is the already-fetched body when resolution had to download it to find
    out whether the selector exists at all, else None. Sources are never inferred:
    a selector no iplist portal carries is an error, not a quiet fall back to
    v2fly — the other source is one prefix away.
    """
    src, portal, sel = parse_selector(name)
    tag = service_tag(name)
    if src == "url":
        return tag, sel, "url", None
    if src in (None, "v2fly"):
        return tag, V2FLY_BASE + sel, "v2fly", None
    found = iplist_find(sel, portal)
    if not found:
        where = portal or ", ".join(p for p, _ in IPLIST_PORTALS)
        raise SystemExit(f"iplist: no site or group {sel!r} on {where}. Try "
                         f"'mtvpn search {sel}', or 'v2fly:{sel}' for the GitHub list.")
    label, url, text = found
    return tag, url, label, text


def service_variants(name):
    """Every (tag, url, source_label, text) a name could install.

    An explicitly sourced name or a URL resolves to exactly one. A bare name is
    looked up in both sources, so `domains <name>` can show what each would give
    before one of them is written into the config.
    """
    src, _, sel = parse_selector(name)
    if src is not None:
        return [resolve_service(name)]
    out = []
    text = fetch(V2FLY_BASE + sel, missing_ok=True)
    if text is not None:
        out.append((sel, V2FLY_BASE + sel, "v2fly", text))
    found = iplist_find(sel)
    if found:
        label, url, itext = found
        out.append((sel, url, label, itext))
    if not out:
        raise SystemExit(f"{sel!r}: no iplist site or group, and not in v2fly's "
                         f"data/. Try 'mtvpn search {sel}'.")
    return out


def parse_list(url, seen=None, text=None):
    """Parse a domain list. Returns (subdomain_domains, exact_domains, skipped).

    Handles: plain domains (match subdomains), full:, domain:, include: (recursive).
    Skips regexp:/keyword: rules — RouterOS cannot express them. iplist's output is
    a subset of this format — bare domains, one per line — so it needs no parser of
    its own, and the same DOMAIN_RE sweep drops the scraped junk it sometimes
    carries ("mailto", "geoffk@apple.com" in the apple group).

    `text`, when given, is the already-fetched body of `url`: resolving an iplist
    selector has to download it to learn whether the selector exists, so it hands
    over what it got instead of making this refetch.
    """
    if seen is None:
        seen = set()
    if url in seen:
        return set(), set(), []
    seen.add(url)

    sub, full, skipped = set(), set(), []
    base = url.rsplit("/", 1)[0] + "/"

    for raw in (fetch(url) if text is None else text).splitlines():
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


def rsc_service(svc, sub, full, cfg, single_scope=True):
    """RouterOS commands that install/refresh one service's domains, idempotently.

    Entries are tagged comment=<svc>. Untagged duplicates (manual entries for the
    same domain) are adopted: removed first, re-added with the tag.

    Two forms, because the two transports parse differently:

    - single_scope=True (default; `/import` fast path and `render`): one `{ ... }`
      block that runs in a single scope, so adoption is O(N) — the domains are
      loaded into a keyed array once and each list is swept a single time with
      O(1) lookups, instead of an O(table) `[find]` per domain.
    - single_scope=False (stdin-pipe fallback): the interactive console evaluates
      each piped line independently (no cross-line scope, and a ~1600-statement
      single-line cap), so we fall back to self-contained per-domain lines. That
      is O(N^2) on the router but only runs when `scp` is unavailable.
    """
    L = cfg["list"]
    # A domain can be in both lists (v2fly `domain:` + `full:` for the same name,
    # e.g. itunes.apple.com). Each name must be added once or the second add
    # fails "entry already exists"; match-subdomain=yes is the superset, so it
    # wins over an exact duplicate.
    domains = [(d, "yes") for d in sorted(sub)] + [(d, "no") for d in sorted(full - sub)]
    if not single_scope:
        out = [
            f'/ip dns static remove [find comment="{svc}" address-list="{L}"]',
            f'/ip firewall address-list remove [find list="{L}" comment="{svc}" dynamic=no]',
        ]
        for domain, match_sub in domains:
            out += [
                f':do {{/ip dns static remove [find name="{domain}" type=FWD address-list="{L}"]}} on-error={{}}',
                f'/ip dns static add name={domain} type=FWD match-subdomain={match_sub} '
                f'address-list={L} comment="{svc}"',
                f':do {{/ip firewall address-list remove [find list="{L}" address="{domain}" dynamic=no]}} on-error={{}}',
                f':do {{/ip firewall address-list add list={L} address={domain} comment="{svc}"}} on-error={{}}',
            ]
        return out
    out = [
        "{",
        f'/ip dns static remove [find comment="{svc}" address-list="{L}"]',
        f'/ip firewall address-list remove [find list="{L}" comment="{svc}" dynamic=no]',
        ':local ours [:toarray ""]',
    ]
    # Short per-line :set statements (not one mega array literal) so the block
    # also survives the interactive-console fallback, which caps line length.
    out += [f':set ($ours->"{domain}") 1' for domain, _ in domains]
    # Adopt untagged duplicates: sweep each list once, remove entries whose
    # name/address is one of ours (same reach as the old per-domain removes).
    out += [
        f':foreach e in=[/ip dns static find where address-list="{L}" type=FWD] '
        f'do={{:if ([:typeof ($ours->[/ip dns static get $e name])]!="nothing") '
        f'do={{/ip dns static remove $e}}}}',
        f':foreach e in=[/ip firewall address-list find where list="{L}" dynamic=no] '
        f'do={{:if ([:typeof ($ours->[/ip firewall address-list get $e address])]!="nothing") '
        f'do={{/ip firewall address-list remove $e}}}}',
    ]
    for domain, match_sub in domains:
        out += [
            f'/ip dns static add name={domain} type=FWD match-subdomain={match_sub} '
            f'address-list={L} comment="{svc}"',
            f':do {{/ip firewall address-list add list={L} address={domain} comment="{svc}"}} on-error={{}}',
        ]
    out.append("}")
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


def scp_base(cfg, local, remote):
    """scp argv to copy `local` to the router as `remote`.

    Either derived from cfg["ssh"] (swap the program to scp, keep pass-through
    options, map ssh's -p PORT to scp's -P PORT, turn the host token into
    host:remote) or, if cfg["scp"] is set, taken from that template with the
    {local}/{remote} placeholders substituted — an escape hatch for topologies
    the derivation gets wrong.
    """
    if cfg.get("scp"):
        return [t.replace("{local}", local).replace("{remote}", remote)
                for t in shlex.split(cfg["scp"])]
    toks = shlex.split(cfg["ssh"])
    if toks and toks[0] == "ssh":
        toks = toks[1:]
    if not toks:
        raise SystemExit('no ssh target: set "ssh:" in the config or pass -r "ssh <host>"')
    host, opts, mapped, i = toks[-1], toks[:-1], [], 0
    while i < len(opts):
        if opts[i] == "-p" and i + 1 < len(opts):  # ssh -p PORT -> scp -P PORT
            mapped += ["-P", opts[i + 1]]
            i += 2
        else:
            mapped.append(opts[i])
            i += 1
    return ["scp", "-o", "BatchMode=yes"] + mapped + [local, f"{host}:{remote}"]


def can_scp(cfg):
    """Whether the /import fast path is usable: some ssh/scp target resolves."""
    return bool(cfg.get("scp") or cfg.get("ssh"))


def target(cfg):
    """Short display name for the router: last token of the ssh command."""
    return cfg["ssh"].split()[-1] if cfg["ssh"] else "(dry-run)"


# /import can exit 0 even when a line failed; scan its output for these too.
IMPORT_ERROR = re.compile(
    r"syntax error|failure|expected|no such item|does not match|bad command|invalid", re.I)


def push(cfg, script, dry_run=False, pipe_script=None):
    """Apply `script` to the router.

    `script` is the single-scope form for the `/import` fast path. `pipe_script`,
    if given, is the per-line form used for the stdin fallback (see rsc_service);
    when omitted the script is self-contained enough to pipe as-is.
    """
    if dry_run:
        print("\n".join(script) + "\n", end="")
        return
    if can_scp(cfg):
        try:
            _push_import(cfg, "\n".join(script) + "\n")
            return
        except FileNotFoundError:  # scp binary missing -> fall back to the pipe
            print("  scp not found; falling back to the stdin pipe (slower)")
    _push_stdin(cfg, "\n".join(pipe_script or script) + "\n")


def _report(lines):
    for l in lines:
        if not SSH_NOISE.search(l):
            print("  router:", l)


def _push_stdin(cfg, text):
    """Fallback transport: pipe the script into the interactive console."""
    r = subprocess.run(
        ssh_base(cfg),
        input=text, capture_output=True, text=True, timeout=300,
    )
    _report((r.stdout + r.stderr).splitlines())
    if r.returncode != 0:
        raise SystemExit(f"ssh to {target(cfg)} failed (exit {r.returncode})")


def _push_import(cfg, text):
    """Fast transport: scp the script to the router and run it with /import."""
    remote = "mtvpn-import.rsc"
    fd, local = tempfile.mkstemp(suffix=".rsc")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        s = subprocess.run(scp_base(cfg, local, remote),
                           capture_output=True, text=True, timeout=300)
        if s.returncode != 0:
            _report((s.stdout + s.stderr).splitlines())
            raise SystemExit(f"scp to {target(cfg)} failed (exit {s.returncode})")
        try:
            r = subprocess.run(
                ssh_base(cfg) + [f"/import file-name={remote} verbose=no"],
                capture_output=True, text=True, timeout=300,
            )
            out = (r.stdout + r.stderr).splitlines()
            _report(out)
            noise_free = [l for l in out if not SSH_NOISE.search(l)]
            if r.returncode != 0 or any(IMPORT_ERROR.search(l) for l in noise_free):
                raise SystemExit(f"/import on {target(cfg)} failed")
        finally:
            # Best-effort router cleanup, even if the import errored out.
            subprocess.run(
                ssh_base(cfg) + [f':do {{/file remove [find name="{remote}"]}} on-error={{}}'],
                capture_output=True, text=True, timeout=120,
            )
    finally:
        os.unlink(local)


def query(cfg, command):
    r = subprocess.run(
        ssh_base(cfg) + [command],
        capture_output=True, text=True, timeout=120,
    )
    return [l for l in r.stdout.splitlines() if not SSH_NOISE.search(l)]


def cmd_bootstrap(cfg, args):
    # Infra lines are self-contained, so they serve both transports; only the
    # per-service blocks differ between the /import and stdin forms.
    infra = rsc_bootstrap(cfg, fix_fasttrack=args.fix_fasttrack)
    script, pipe_script = list(infra), list(infra)
    services = expand_services(cfg)
    for name in services:
        svc, url, source, text = resolve_service(name)
        sub, full, skipped = parse_list(url, text=text)
        report_parse(svc, sub, full, skipped, source)
        script += rsc_service(svc, sub, full, cfg)
        pipe_script += rsc_service(svc, sub, full, cfg, single_scope=False)
    push(cfg, script, args.dry_run, pipe_script=pipe_script)
    if not args.dry_run:
        print(f"bootstrapped {target(cfg)}: infra + {len(services)} service(s)")
        print("NOTE: LAN clients must use the router as their only DNS server "
              "(DHCP dns-server, /ip dns allow-remote-requests=yes), or the "
              "FWD-based subdomain coverage will not populate the list.")


def named_services(args):
    """The services one add/update/remove invocation covers: those named on the
    command line, then everything --from-list carries, deduped by tag."""
    names = list(args.services)
    for src in (getattr(args, "from_list", None) or []):
        names += read_service_list(src)
    seen, out = set(), []
    for name in names:
        tag = service_tag(name)
        if tag not in seen:
            seen.add(tag)
            out.append(name)
    return out


def record_config(cfg, config_path, args):
    """Write back what `add` installed: the --from-list URLs, then the explicitly
    named services the config's lists don't already carry.

    Dedupe is by tag, not by spelling, at both levels: `add anthropic` must not
    append a second entry when the config (or one of its lists) already carries
    it as v2fly:anthropic.
    """
    changed = False
    for src in (args.from_list or []):
        if src not in cfg["service_lists"]:
            cfg["service_lists"].append(src)
            changed = True
    covered = {service_tag(n) for n in cfg["services"]}
    try:
        covered |= {service_tag(n) for src in cfg["service_lists"]
                    for n in read_service_list(src)}
    except SystemExit:  # a list unreadable right now must not lose the bookkeeping
        pass
    for name in args.services:
        if service_tag(name) not in covered:
            cfg["services"].append(name)
            covered.add(service_tag(name))
            changed = True
    if changed:
        save_config(config_path, cfg)


def cmd_add(cfg, args, config_path, persist=True):
    """Install/refresh services. persist=False for `update`, which is a refresh of
    what the config already points at and must not graft a one-off list into it."""
    services = named_services(args)
    if not services:
        raise SystemExit("nothing to add: no services given and no list entries")
    for name in services:
        svc, url, source, text = resolve_service(name)
        sub, full, skipped = parse_list(url, text=text)
        report_parse(svc, sub, full, skipped, source)
        push(cfg, rsc_service(svc, sub, full, cfg), args.dry_run,
             pipe_script=rsc_service(svc, sub, full, cfg, single_scope=False))
        if not args.dry_run:
            print(f"added '{svc}' ({len(sub) + len(full)} domains) on {target(cfg)}")
    if persist and not args.dry_run and Path(config_path).exists():
        record_config(cfg, config_path, args)


def cmd_update(cfg, args, config_path):
    # Pruning against a subset of the services would delete every service not
    # named, so it is only offered for the full refresh — --urls-only is such a
    # subset, and pruning against it would sweep every v2fly/iplist service.
    if (args.services or args.urls_only) and args.prune:
        raise SystemExit("--prune only applies to a full update "
                         "(no service arguments, no --urls-only)")
    # No explicit services: refresh the whole effective set — config services plus
    # everything the hosted lists carry, which is how a list edited on the server
    # reaches the router.
    args.services = args.services or expand_services(cfg, args.from_list or [])
    if args.urls_only:
        # Your own domain lists change far more often than the curated upstreams,
        # so this refreshes just them instead of re-fetching every service.
        args.services = [n for n in args.services if parse_selector(n)[0] == "url"]
        if not args.services:
            raise SystemExit("nothing to update: no raw-URL domain lists among the services")
    if not args.services:
        raise SystemExit("nothing to update: no services given and none in config")
    cmd_add(cfg, args, config_path, persist=False)
    if args.prune:
        prune_services(cfg, args)


def prune_services(cfg, args):
    """Drop router services no longer in the effective set — how a service dropped
    from a hosted list stops being tunneled."""
    if args.dry_run or not cfg["ssh"]:
        print("# --prune needs the router; skipped", file=sys.stderr)
        return
    stale = router_service_tags(cfg) - {service_tag(n) for n in args.services}
    for svc in sorted(stale):
        push(cfg, rsc_remove_service(svc, cfg))
        print(f"pruned '{svc}' from {target(cfg)}")


def cmd_remove(cfg, args, config_path):
    services = named_services(args)
    if not services:
        raise SystemExit("nothing to remove: no services given and no list entries")
    for name in services:
        svc = service_tag(name)  # tag only: removing must never need the network
        push(cfg, rsc_remove_service(svc, cfg), args.dry_run)
        if not args.dry_run:
            print(f"removed '{svc}' from {target(cfg)}")
        if not args.dry_run and Path(config_path).exists():
            cfg["services"] = [s for s in cfg["services"] if service_tag(s) != svc]
            save_config(config_path, cfg)
    if not args.dry_run and Path(config_path).exists() and (args.from_list or []):
        # the list itself goes too, else the next update reinstalls everything
        cfg["service_lists"] = [s for s in cfg["service_lists"] if s not in args.from_list]
        save_config(config_path, cfg)


def router_service_tags(cfg):
    """Service tags installed on the router, read off the DNS static entries.

    DNS static and not the address list, so the router-side telegram-cidr script
    (address-list entries only) is invisible here and `--prune` leaves it alone.
    Untagged entries yield "" and are dropped: their tag would match every
    adopted entry.
    """
    lines = query(
        cfg,
        ':foreach i in=[/ip dns static find where address-list="%s"] '
        'do={:put [:tostr [/ip dns static get $i comment]]}' % cfg["list"],
    )
    return {l.strip() for l in lines if l.strip()}


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
        svc, url, source, text = resolve_service(name)
        sub, full, skipped = parse_list(url, text=text)
        report_parse(svc, sub, full, skipped, source)
        print("\n".join(rsc_service(svc, sub, full, cfg)))


def cmd_domains(cfg, args):
    """Print the resolved domains for service(s) upstream. No router needed.

    Subdomain-match domains print as-is; exact (v2fly full:) domains are prefixed
    `full:` so the two match kinds stay distinguishable. A bare name prints one
    block per source that carries it — the headers naming each source go to stderr
    like every other diagnostic, so stdout stays a plain domain list to pipe.
    """
    for name in args.services:
        for svc, url, source, text in service_variants(name):
            sub, full, skipped = parse_list(url, text=text)
            report_parse(svc, sub, full, skipped, source)
            for d in sorted(sub):
                print(d)
            for d in sorted(full):
                print(f"full:{d}")


def list_services():
    """Available v2fly service names (the filenames under data/).

    Uses the git trees API, not contents: the data/ dir exceeds the contents
    API's 1000-entry page cap and would silently truncate.
    """
    root = json.loads(fetch(V2FLY_TREE_API + "master"))
    data_sha = next(e["sha"] for e in root["tree"]
                    if e["path"] == "data" and e["type"] == "tree")
    tree = json.loads(fetch(V2FLY_TREE_API + data_sha))
    return sorted(e["path"] for e in tree["tree"] if e["type"] == "blob")


def iplist_catalog():
    """Every (portal, group, site) triple iplist serves.

    format=custom with a {group}|{site} template is the only endpoint that exposes
    group membership; plain ?format=json does too, but ships the whole 40 MB config
    dump to do it. It emits one line per domain, hence the dedupe. A site with no
    wildcard domains at all never appears, which costs nothing — there would be
    nothing to add from it anyway.
    """
    out = set()
    for name, base in IPLIST_PORTALS:
        url = base + "?" + urllib.parse.urlencode(
            {"format": "custom", "data": "domains", "wildcard": "1",
             "template": "{group}|{site}"})
        try:
            text = fetch(url)
        except OSError as e:
            print(f"# iplist portal {name} unreachable: {e}", file=sys.stderr)
            continue
        for line in text.splitlines():
            group, sep, site = line.strip().partition("|")
            if sep:
                out.add((name, group, site))
    return sorted(out)


def cmd_search(cfg, args):
    """List selectors from both sources, spelled the way a config entry is."""
    q = args.query.lower() if args.query else None
    rows = []
    if args.source in (None, "iplist"):
        catalog = iplist_catalog()
        sites = {}
        for portal, group, site in catalog:
            sites[(portal, group)] = sites.get((portal, group), 0) + 1
        rows += [(f"iplist:{g}", p, "group", f"{n} site(s)") for (p, g), n in sites.items()]
        rows += [(f"iplist:{s}", p, "site", f"({g})") for p, g, s in catalog]
    if args.source in (None, "v2fly"):
        try:
            rows += [(f"v2fly:{n}", "", "", "") for n in list_services()]
        except OSError as e:  # a GitHub outage/rate-limit shouldn't hide iplist's half
            print(f"# v2fly listing unavailable: {e}", file=sys.stderr)
    matched = [r for r in rows if q is None or q in r[0].lower()]
    for ident, portal, kind, extra in sorted(matched):
        print(f"{ident:<34}{portal:<8}{kind:<7}{extra}".rstrip())
    if q is not None:
        print(f"# {len(matched)} match(es) for {args.query!r}", file=sys.stderr)


def report_parse(svc, sub, full, skipped, source=None):
    src = f" [{source}]" if source else ""
    print(f"# {svc}{src}: {len(sub)} subdomain-match + {len(full)} exact domains",
          file=sys.stderr)
    for s in skipped:
        print(f"#   skipped (unsupported on RouterOS): {s}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="Selective-VPN domain routing on MikroTik from iplist/v2fly domain lists")
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
                 ("render", "print RouterOS commands for service(s) to stdout"),
                 ("domains", "print resolved domains for service(s) upstream to stdout")]:
        p = sp.add_parser(c, help=h)
        p.add_argument("services", nargs="*" if c in ("update", "add", "remove") else "+",
                       help="iplist:<site|group>, v2fly:<name>, or a raw URL of a "
                            "domain list, optionally named <tag>=<url> "
                            "(a bare name means v2fly:)")
        if c in ("add", "update", "remove"):
            p.add_argument("-l", "--from-list", metavar="URL", action="append",
                           help="URL or path of a hosted service list (one selector "
                                "per line); repeatable. add/remove also record it "
                                "in the config's service_lists:")
        if c == "update":
            p.add_argument("--urls-only", action="store_true",
                           help="only refresh services whose source is a raw URL "
                                "domain list, skipping the iplist/v2fly ones")
            p.add_argument("--prune", action="store_true",
                           help="also remove router services that are no longer in "
                                "the config or its lists")

    p = sp.add_parser("search", help="list selectors available from iplist and v2fly")
    p.add_argument("query", nargs="?", help="substring to filter selectors")
    p.add_argument("-s", "--source", choices=SOURCES, help="only search this source")

    p = sp.add_parser("list", help="show services installed on the router")
    p.add_argument("-v", "--verbose", action="store_true", help="also list domains")

    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.router:
        cfg["ssh"] = args.router
    if getattr(args, "gateway", None):
        cfg["gateway"] = args.gateway
    if args.cmd not in ("render", "domains", "search") and not cfg["ssh"] and not args.dry_run:
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
    elif args.cmd == "domains":
        cmd_domains(cfg, args)
    elif args.cmd == "search":
        cmd_search(cfg, args)


if __name__ == "__main__":
    main()
