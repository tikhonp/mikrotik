#!/usr/bin/env python3
"""mtvpn — manage the selective-VPN domain list on MikroTik RouterOS 7.

mtvpn owns one thing: what is in the firewall address-list (default to_vpn_list).
The routing that acts on it — mangle marks, routing table, fail-open default
route, the DoH forwarder — is installed once by fresh-router.rsc.

Per domain it writes two objects: an /ip dns static FWD entry (match-subdomain,
address-list=, forward-to=<doh_forwarder> so the name resolves through the
tunnel) and an /ip firewall address-list hostname entry for the apex.

Sources are named explicitly and never inferred: iplist:<site|group>,
v2fly:<name>, a raw URL, or a bare name (= v2fly:). Requires python3 and ssh
key auth to the router.
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

# Near-disjoint catalogs, probed in this order; iplist:<portal>:<selector> pins one.
IPLIST_PORTALS = [
    ("main", "https://iplist.opencck.org/"),
    ("beta", "https://beta.iplist.opencck.org/"),
    ("russia", "https://russia.iplist.opencck.org/"),
]
SOURCES = ("iplist", "v2fly")

# `<tag>=<url>`: only splits when a bare tag precedes a URL scheme, so a plain
# URL carrying a query string is never split.
NAMED_URL_RE = re.compile(r"^([a-z0-9][a-z0-9._-]*)=(?=[a-z][a-z0-9+.-]*://)", re.I)
TAG_EXTENSIONS = (".txt", ".list", ".lst", ".dat", ".conf", ".md")

DEFAULTS = {
    "ssh": "",                # full ssh command, e.g. "ssh -J jumphost 10.0.0.1"
    "scp": "",                # optional scp override, {local}/{remote} placeholders
    "list": "to_vpn_list",    # address-list the router tunnels; from fresh-router.rsc
    "doh_forwarder": "vpn-doh",  # /ip dns forwarders name; from fresh-router.rsc
    # A full refresh writes ~2 objects per domain; a client-side timeout would
    # abort /import partway and leave a service half-removed.
    "push_timeout": 1800,
    "service_lists": [],
    "services": [],
}

SSH_NOISE = re.compile(r"WARNING|post-quantum|store now|upgraded|^\s*$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

SERVICES_HELP = ("iplist:<site|group>, v2fly:<name>, or a raw URL of a domain list, "
                 "optionally named <tag>=<url> (a bare name means v2fly:)")


def parse_yaml(text):
    """Flat `key: value` scalars plus one-level `- item` lists. Full-line comments only."""
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
    for key in ("services", "service_lists"):  # an empty "key:" line parses as ""
        if isinstance(cfg.get(key), str):
            cfg[key] = [cfg[key]] if cfg[key] else []
    try:
        cfg["push_timeout"] = int(cfg["push_timeout"])
    except (TypeError, ValueError):
        raise SystemExit(f"config: push_timeout must be a number, got {cfg['push_timeout']!r}")
    return cfg


def save_config(path, cfg):
    Path(path).write_text(dump_yaml({k: cfg[k] for k in DEFAULTS if k in cfg}))


def split_named_url(name):
    """Split the optional `<tag>=<url>` form into (tag or None, rest)."""
    m = NAMED_URL_RE.match(name)
    return (m.group(1).lower(), name[m.end():]) if m else (None, name)


def parse_selector(name):
    """Split a config entry into (source, portal, selector).

    source is "iplist", "v2fly", "url", or None for a bare name (treated as v2fly).
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
    if portal not in {p for p, _ in IPLIST_PORTALS}:
        raise SystemExit(f"unknown iplist portal {portal!r}: expected one of "
                         f"{', '.join(p for p, _ in IPLIST_PORTALS)}")
    return "iplist", portal, rest2.strip()


def service_tag(name):
    """Router comment tag for a config entry: its selector, minus the source prefix.

    Network-free by contract, and the unit everything dedupes by: `add anthropic`
    must not append a second entry beside an existing v2fly:anthropic.
    """
    src, _, sel = parse_selector(name)
    if src != "url":
        return sel
    tag, _ = split_named_url(name.strip())
    if tag:
        return tag
    q = urllib.parse.parse_qs(urllib.parse.urlparse(sel).query)
    for kind in ("site", "group"):  # an iplist URL carries its selector in the query
        if q.get(kind):
            return q[kind][0].lower()
    parts = urllib.parse.urlparse(sel)
    # Never fall through to "": the empty tag is what adopted entries carry.
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
    """Read a hosted list of service *selectors* (not domains), one per line.

    A `services:` block pastes in verbatim: a leading "- " is stripped. `#`
    comments only at line start or after whitespace, so raw-URL entries survive.
    Cached per run — a hosted list must not change mid-command.
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
        if line.startswith("- "):
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
    """cfg["services"] plus every hosted list's entries; the config spelling wins."""
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

    wildcard=1 is not optional: wildcard=0 returns every hostname ever observed
    (15k+ for youtube), while the wildcard set is the apex list, which is exactly
    what RouterOS match-subdomain=yes means.
    """
    return base + "?" + urllib.parse.urlencode(
        {"format": "text", "data": "domains", "wildcard": "1", kind: selector})


def iplist_find(selector, portal=None):
    """Locate an iplist selector. Returns (source_label, url, text) or None.

    A portal without the selector answers 200 with an empty body, not 404, so
    emptiness is the only miss signal — which means an unreachable portal must
    not read as a miss.
    """
    for name, base in [(p, b) for p, b in IPLIST_PORTALS if portal in (None, p)]:
        for kind in ("site", "group"):  # shape doesn't tell them apart: sites can be dotless
            url = iplist_url(base, kind, selector)
            try:
                text = fetch(url)
            except OSError as e:
                print(f"# iplist portal {name} unreachable: {e}", file=sys.stderr)
                break
            if text.strip():
                return f"iplist {name} {kind}", url, text
    return None


def resolve_service(name):
    """Map a config entry to (tag, url, source_label, text).

    `text` is the already-fetched body when resolution had to download it to
    learn whether the selector exists, else None.
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
    """Every (tag, url, source_label, text) a name could install: a bare name has one per source."""
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
    """Parse a domain list into (subdomain_domains, exact_domains, skipped).

    Handles plain domains, full:, domain:, include: (recursive); skips
    regexp:/keyword:, which RouterOS cannot express. iplist's bare-domain output
    is a subset of this format, and the DOMAIN_RE sweep drops the scraped junk it
    carries. `text`, when given, is the already-fetched body of `url`.
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


def rsc_service(svc, sub, full, cfg):
    """RouterOS script that installs/refreshes one service's domains, idempotently.

    Entries are tagged comment=<svc>; untagged entries for the same domains are
    adopted (removed, then re-added tagged). One `{ }` block so the keyed array
    makes adoption O(N) instead of a [find] per domain.
    """
    L = cfg["list"]
    fwd = f'forward-to={cfg["doh_forwarder"]} ' if cfg["doh_forwarder"] else ""
    # A name can be in both lists (v2fly domain: + full:); match-subdomain=yes is
    # the superset and wins, and each name must be added exactly once.
    domains = [(d, "yes") for d in sorted(sub)] + [(d, "no") for d in sorted(full - sub)]
    out = [
        "{",
        f'/ip dns static remove [find comment="{svc}" address-list="{L}"]',
        f'/ip firewall address-list remove [find list="{L}" comment="{svc}" dynamic=no]',
        ':local ours [:toarray ""]',
    ]
    # One :set per line rather than one array literal: the console caps line length.
    out += [f':set ($ours->"{domain}") 1' for domain, _ in domains]
    out += [
        f':foreach e in=[/ip dns static find where address-list="{L}" type=FWD] '
        f'do={{:if ([:typeof ($ours->[/ip dns static get $e name])]!="nothing") '
        f'do={{/ip dns static remove $e}}}}',
        # mtvpn:-commented entries are infra pins from fresh-router.rsc (the DoH
        # resolver, the Telegram fetch host), not adoptable duplicates.
        f':foreach e in=[/ip firewall address-list find where list="{L}" dynamic=no] '
        f'do={{:if ([:typeof ($ours->[/ip firewall address-list get $e address])]!="nothing" '
        f'&& [:pick [:tostr [/ip firewall address-list get $e comment]] 0 6]!="mtvpn:") '
        f'do={{/ip firewall address-list remove $e}}}}',
    ]
    for domain, match_sub in domains:
        out += [
            f'/ip dns static add name={domain} type=FWD match-subdomain={match_sub} '
            f'{fwd}address-list={L} comment="{svc}"',
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


def ssh_base(cfg):
    cmd = shlex.split(cfg["ssh"])
    if not cmd:
        raise SystemExit('no ssh target: set "ssh:" in the config or pass -r "ssh <host>"')
    if cmd[0] != "ssh":
        cmd.insert(0, "ssh")
    return [cmd[0], "-o", "BatchMode=yes"] + cmd[1:]


def scp_base(cfg, local, remote):
    """scp argv to copy `local` to the router as `remote`.

    Derived from cfg["ssh"] (program swapped, ssh's -p PORT mapped to scp's -P
    PORT, host token turned into host:remote), or taken from cfg["scp"] as a
    template with {local}/{remote} substituted.
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
        if opts[i] == "-p" and i + 1 < len(opts):
            mapped += ["-P", opts[i + 1]]
            i += 2
        else:
            mapped.append(opts[i])
            i += 1
    return ["scp", "-o", "BatchMode=yes"] + mapped + [local, f"{host}:{remote}"]


def target(cfg):
    return cfg["ssh"].split()[-1]


# /import can exit 0 even when a line failed; scan its output for these too.
IMPORT_ERROR = re.compile(
    r"syntax error|failure|expected|no such item|does not match|bad command|invalid", re.I)


def push(cfg, script, dry_run=False):
    if dry_run:
        print("\n".join(script) + "\n", end="")
        return
    _push_import(cfg, "\n".join(script) + "\n")


def _report(lines):
    for l in lines:
        if not SSH_NOISE.search(l):
            print("  router:", l)


def _push_import(cfg, text):
    """scp the script to the router and run it with /import."""
    remote = "mtvpn-import.rsc"
    fd, local = tempfile.mkstemp(suffix=".rsc")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        s = subprocess.run(scp_base(cfg, local, remote),
                           capture_output=True, text=True, timeout=cfg["push_timeout"])
        if s.returncode != 0:
            _report((s.stdout + s.stderr).splitlines())
            raise SystemExit(f"scp to {target(cfg)} failed (exit {s.returncode})")
        try:
            r = subprocess.run(
                ssh_base(cfg) + [f"/import file-name={remote} verbose=no"],
                capture_output=True, text=True, timeout=cfg["push_timeout"],
            )
            out = (r.stdout + r.stderr).splitlines()
            _report(out)
            noise_free = [l for l in out if not SSH_NOISE.search(l)]
            if r.returncode != 0 or any(IMPORT_ERROR.search(l) for l in noise_free):
                raise SystemExit(f"/import on {target(cfg)} failed")
        finally:
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


def named_services(args):
    """Services this invocation covers: those named on the command line, then
    everything --from-list carries."""
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


def record_config(cfg, args):
    """Write back what `add` installed: the --from-list URLs, then the named
    services the config's lists don't already carry."""
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
        save_config(args.config, cfg)


def cmd_add(cfg, args, persist=True):
    """persist=False for `update`, which must not graft a one-off list into the config."""
    services = named_services(args)
    if not services:
        raise SystemExit("nothing to add: no services given and no list entries")
    for name in services:
        svc, url, source, text = resolve_service(name)
        sub, full, skipped = parse_list(url, text=text)
        report_parse(svc, sub, full, skipped, source)
        push(cfg, rsc_service(svc, sub, full, cfg), args.dry_run)
        if not args.dry_run:
            print(f"added '{svc}' ({len(sub) + len(full)} domains) on {target(cfg)}")
    if persist and not args.dry_run and Path(args.config).exists():
        record_config(cfg, args)


def cmd_update(cfg, args):
    # Pruning against a subset would delete every service not named.
    if (args.services or args.urls_only) and args.prune:
        raise SystemExit("--prune only applies to a full update "
                         "(no service arguments, no --urls-only)")
    args.services = args.services or expand_services(cfg, args.from_list or [])
    if args.urls_only:
        args.services = [n for n in args.services if parse_selector(n)[0] == "url"]
        if not args.services:
            raise SystemExit("nothing to update: no raw-URL domain lists among the services")
    if not args.services:
        raise SystemExit("nothing to update: no services given and none in config")
    cmd_add(cfg, args, persist=False)
    if args.prune:
        prune_services(cfg, args)


def prune_services(cfg, args):
    if args.dry_run or not cfg["ssh"]:
        print("# --prune needs the router; skipped", file=sys.stderr)
        return
    stale = router_service_tags(cfg) - {service_tag(n) for n in args.services}
    for svc in sorted(stale):
        push(cfg, rsc_remove_service(svc, cfg))
        print(f"pruned '{svc}' from {target(cfg)}")


def cmd_remove(cfg, args):
    services = named_services(args)
    if not services:
        raise SystemExit("nothing to remove: no services given and no list entries")
    for name in services:
        svc = service_tag(name)  # tag only: removing must never need the network
        push(cfg, rsc_remove_service(svc, cfg), args.dry_run)
        if not args.dry_run:
            print(f"removed '{svc}' from {target(cfg)}")
        if not args.dry_run and Path(args.config).exists():
            cfg["services"] = [s for s in cfg["services"] if service_tag(s) != svc]
            save_config(args.config, cfg)
    if not args.dry_run and Path(args.config).exists() and (args.from_list or []):
        # the list itself goes too, else the next update reinstalls everything
        cfg["service_lists"] = [s for s in cfg["service_lists"] if s not in args.from_list]
        save_config(args.config, cfg)


def router_service_tags(cfg):
    """Service tags on the router, read off the DNS static entries — so the
    address-list-only telegram-cidr entries stay invisible to --prune. Untagged
    entries yield "" and are dropped: that tag matches every adopted entry."""
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
    services: dict = {}
    for l in lines:
        if "|" in l:
            svc, name = l.split("|", 1)
            services.setdefault(svc or "(untagged)", []).append(name)
    for svc in sorted(services):
        print(f"{svc}: {len(services[svc])} domains")
        if args.verbose:
            for n in sorted(services[svc]):
                print(f"   {n}")


def cmd_domains(cfg, args):
    """Print a service's domains upstream. Exact (full:) domains keep the prefix;
    the source headers go to stderr so stdout stays a plain pipeable list."""
    for name in args.services:
        for svc, url, source, text in service_variants(name):
            sub, full, skipped = parse_list(url, text=text)
            report_parse(svc, sub, full, skipped, source)
            for d in sorted(sub):
                print(d)
            for d in sorted(full):
                print(f"full:{d}")


def list_services():
    """v2fly service names (the filenames under data/), via the git trees API —
    the contents API truncates at 1000 entries."""
    root = json.loads(fetch(V2FLY_TREE_API + "master"))
    data_sha = next(e["sha"] for e in root["tree"]
                    if e["path"] == "data" and e["type"] == "tree")
    tree = json.loads(fetch(V2FLY_TREE_API + data_sha))
    return sorted(e["path"] for e in tree["tree"] if e["type"] == "blob")


def iplist_catalog():
    """Every (portal, group, site) triple iplist serves.

    format=custom is the only endpoint exposing group membership without shipping
    the 40 MB config dump ?format=json would. One line per domain, hence the dedupe.
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
        sites: dict = {}
        for portal, group, site in catalog:
            sites[(portal, group)] = sites.get((portal, group), 0) + 1
        rows += [(f"iplist:{g}", p, "group", f"{n} site(s)") for (p, g), n in sites.items()]
        rows += [(f"iplist:{s}", p, "site", f"({g})") for p, g, s in catalog]
    if args.source in (None, "v2fly"):
        try:
            rows += [(f"v2fly:{n}", "", "", "") for n in list_services()]
        except OSError as e:  # a GitHub outage shouldn't hide iplist's half
            print(f"# v2fly listing unavailable: {e}", file=sys.stderr)
    matched = [r for r in rows if q is None or q in r[0].lower()]
    for ident, portal, kind, extra in sorted(matched):
        print(f"{ident:<34}{portal:<8}{kind:<7}{extra}".rstrip())
    if q is not None:
        print(f"# {len(matched)} match(es) for {args.query!r}", file=sys.stderr)


def report_parse(svc, sub, full, skipped, source):
    print(f"# {svc} [{source}]: {len(sub)} subdomain-match + {len(full)} exact domains",
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

    def service_parser(name, help_text):
        p = sp.add_parser(name, help=help_text)
        p.add_argument("services", nargs="*", help=SERVICES_HELP)
        p.add_argument("-l", "--from-list", metavar="URL", action="append",
                       help="URL or path of a hosted service list (one selector per line); "
                            "repeatable. add/remove also record it in service_lists:")
        return p

    service_parser("add", "fetch service list(s) and install (or refresh) them"
                   ).set_defaults(func=cmd_add)
    p = service_parser("update", "re-fetch and refresh services (default: all from config)")
    p.add_argument("--urls-only", action="store_true",
                   help="only refresh services whose source is a raw URL domain list")
    p.add_argument("--prune", action="store_true",
                   help="also remove router services no longer in the config or its lists")
    p.set_defaults(func=cmd_update)
    service_parser("remove", "remove service(s) from the router").set_defaults(func=cmd_remove)

    p = sp.add_parser("domains", help="print resolved domains for service(s) upstream to stdout")
    p.add_argument("services", nargs="+", help=SERVICES_HELP)
    p.set_defaults(func=cmd_domains)

    p = sp.add_parser("search", help="list selectors available from iplist and v2fly")
    p.add_argument("query", nargs="?", help="substring to filter selectors")
    p.add_argument("-s", "--source", choices=SOURCES, help="only search this source")
    p.set_defaults(func=cmd_search)

    p = sp.add_parser("list", help="show services installed on the router")
    p.add_argument("-v", "--verbose", action="store_true", help="also list domains")
    p.set_defaults(func=cmd_list)

    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.router:
        cfg["ssh"] = args.router
    if args.cmd not in ("domains", "search") and not cfg["ssh"] and not args.dry_run:
        raise SystemExit('no router: use -r "ssh <host>" or set "ssh:" in the config file')
    args.func(cfg, args)


if __name__ == "__main__":
    main()
