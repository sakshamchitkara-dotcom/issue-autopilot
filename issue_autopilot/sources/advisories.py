"""Known vulnerabilities: GitHub Dependabot alerts when readable, else OSV.dev for exact pins."""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from ..github import GitHubError, http_get_json, http_post_json
from ..models import Signal
from . import Context, iter_text_files
from .deps import bounds, manifest_deps, version_tuple

OSV = "https://api.osv.dev/v1"
OSV_ECOSYSTEM = {"pypi": "PyPI", "npm": "npm"}
URGENT = {"critical", "high"}
OSV_BATCH = 1000  # querybatch accepts at most 1000 queries
LOCKFILE_BYTES = 50_000_000  # lockfiles are routinely over the 1 MB text-scan limit


def _signal(manifest: str, pkg: str, version: str, vid: str, summary: str, severity: str,
            fixed: str | None, url: str) -> Signal:
    fix = f"; fixed in {fixed}" if fixed else "; no fix released"
    return Signal(
        kind="advisory",
        group=manifest,
        summary=f"{pkg} {version}: {vid} {summary.strip()} ({severity or 'unknown'} severity{fix})",
        priority="P1" if severity.lower() in URGENT else "P2",
        path=manifest,
        meta={"package": pkg, "advisory": vid, "severity": severity, "fixed": fixed, "url": url},
    )


def from_dependabot(ctx: Context) -> list[Signal]:
    signals = []
    for a in ctx.gh.paginate(f"/repos/{ctx.repo}/dependabot/alerts?state=open"):
        dep, adv = a.get("dependency") or {}, a.get("security_advisory") or {}
        vuln = a.get("security_vulnerability") or {}
        signals.append(_signal(
            dep.get("manifest_path") or "dependencies", (dep.get("package") or {}).get("name", "?"),
            vuln.get("vulnerable_version_range", ""), adv.get("ghsa_id", "?"), adv.get("summary", ""),
            adv.get("severity", ""), (vuln.get("first_patched_version") or {}).get("identifier"),
            a.get("html_url", "")))
    return signals


def _fixed_after(vuln: dict, pkg: str, version: str) -> str | None:
    cur = version_tuple(version)
    fixes = [e["fixed"] for aff in vuln.get("affected", [])
             if (aff.get("package") or {}).get("name", "").lower() == pkg.lower()
             for r in aff.get("ranges", []) for e in r.get("events", []) if "fixed" in e]
    later = sorted((f for f in fixes if version_tuple(f) > cur), key=version_tuple)
    return later[0] if later else None


def _npm_lock(text: str) -> list[tuple[str, str]]:
    """(name, version) from package-lock.json: v2/v3 "packages", else v1's nested "dependencies"."""
    data = json.loads(text)
    if "packages" in data:
        return [(key.rsplit("node_modules/", 1)[-1], p["version"]) for key, p in data["packages"].items()
                if key and "version" in p and not p.get("link")]
    out, stack = [], [data.get("dependencies") or {}]
    while stack:
        for name, p in stack.pop().items():
            if "version" in p:
                out.append((name, p["version"]))
            stack.append(p.get("dependencies") or {})
    return out


def _toml_lock(text: str) -> list[tuple[str, str]]:
    """(name, version) from poetry.lock / uv.lock, skipping local, git and editable packages."""
    out = []
    for p in tomllib.loads(text).get("package") or []:
        src = p.get("source")
        if isinstance(src, dict) and not ("registry" in src or src.get("type") == "legacy"):
            continue
        if "name" in p and "version" in p:
            out.append((p["name"], p["version"]))
    return out


def _yarn_name(spec: str) -> str | None:
    """Registry package behind a yarn.lock key, or None when it isn't a registry release.

    `alias@npm:real@^1` installs `real`, not `alias`. Git, GitHub shorthand (`user/repo`), tarball URLs and
    workspace, link, portal, file and patch protocols are skipped."""
    at = spec.find("@", 1)
    if at < 0:
        return None
    name, rng = spec[:at], spec[at + 1:]
    if rng.startswith("npm:"):
        rng = rng[4:]
        if (i := rng.find("@", 1)) > 0:
            name, rng = rng[:i], rng[i + 1:]
    return None if re.match(r"[a-z+]+:", rng) or "/" in rng else name


def _yarn_lock(text: str) -> list[tuple[str, str]]:
    """(name, version) from yarn.lock, classic (`version "1.2.3"`) and berry (`version: 1.2.3`).

    Entries that aren't registry releases are skipped (see `_yarn_name`)."""
    out, name = [], None
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":") and not line.startswith("#"):
            spec = line.rstrip()[:-1].split(",")[0].strip().strip('"')
            name = _yarn_name(spec)
        elif name and (m := re.match(r'\s+version:?\s+"?([^"\s]+)"?\s*$', line)):
            out.append((name, m.group(1)))
            name = None
    return out


def _pnpm_lock(text: str) -> list[tuple[str, str]]:
    """(name, version) from the `packages:` keys of pnpm-lock.yaml: v5 `/name/1.2.3_peer`,
    v6 `/name@1.2.3(peer)`, v9 `name@1.2.3`. Non-registry versions (link:, file:, git) are skipped."""
    out, inside = [], False
    for line in text.splitlines():
        if line and not line[0].isspace():
            inside = line.rstrip() == "packages:"
        elif inside and (m := re.match(r"^  ([^\s].*):\s*$", line)):
            key = m.group(1).strip("'\"").lstrip("/").split("(")[0]
            if v5 := re.match(r"^((?:@[^/]+/)?[^/@]+)/(\d[^/_]*)", key):  # v5: peers follow a `_`
                name, version = v5.groups()
            else:
                name, _, version = key.rpartition("@")
            if name and re.match(r"\d", version):
                out.append((name, version))
    return out


LOCKFILES = {"package-lock.json": ("npm", _npm_lock), "yarn.lock": ("npm", _yarn_lock),
             "pnpm-lock.yaml": ("npm", _pnpm_lock), "poetry.lock": ("pypi", _toml_lock),
             "uv.lock": ("pypi", _toml_lock)}


def lockfile_pins(ctx: Context) -> list[tuple[str, str, str, str]]:
    """(lockfile, eco, name, version) for every locked package: covers ranges and transitive deps."""
    pins = []
    for rel, text in iter_text_files(ctx.path, ctx.options.get("exclude"), max_bytes=LOCKFILE_BYTES):
        kind = LOCKFILES.get(rel.rsplit("/", 1)[-1])
        if not kind:
            continue
        try:
            pins += [(rel, kind[0], n, v) for n, v in kind[1](text)]
        except (ValueError, KeyError, TypeError, tomllib.TOMLDecodeError) as e:
            ctx.warnings.append(f"advisory: could not parse {rel} ({e}); skipped")
    return pins


def from_osv(ctx: Context) -> list[Signal]:
    pins = []  # (file, eco, name, version) -- OSV needs one concrete version, so ranges come from lockfiles
    for manifest, eco, name, spec in manifest_deps(ctx):
        if eco in OSV_ECOSYSTEM and len(b := bounds(spec)) == 1 and b[0][3]:
            pins.append((manifest, eco, name, re.sub(r"^=+", "", spec).strip()))
    seen_pins: set = set()
    pins = [p for p in pins + lockfile_pins(ctx)
            if (k := (p[1], p[2].lower(), p[3])) not in seen_pins and not seen_pins.add(k)]
    if not pins:
        return []
    queries = [{"package": {"name": n, "ecosystem": OSV_ECOSYSTEM[e]}, "version": v} for _, e, n, v in pins]
    results = [r for i in range(0, len(queries), OSV_BATCH)
               for r in http_post_json(f"{OSV}/querybatch", {"queries": queries[i:i + OSV_BATCH]})["results"]]
    ids = sorted({v["id"] for r in results for v in r.get("vulns") or []})
    with ThreadPoolExecutor(max_workers=8) as pool:  # errors propagate: a partial answer isn't safe to act on
        details = dict(zip(ids, pool.map(lambda i: http_get_json(f"{OSV}/vulns/{i}"), ids)))

    signals = []
    for (manifest, _, name, version), r in zip(pins, results):
        seen: set[str] = set()
        # GHSA first: OSV lists the same flaw under GHSA and PYSEC ids, keep one of them.
        for vid in sorted((v["id"] for v in r.get("vulns") or []), key=lambda i: (not i.startswith("GHSA"), i)):
            if vid in seen:
                continue
            d = details[vid]
            seen |= {vid, *d.get("aliases", [])}
            severity = (d.get("database_specific") or {}).get("severity", "")
            summary = d.get("summary") or (d.get("details") or "").split("\n")[0][:120]
            signals.append(_signal(manifest, name, version, vid, summary, severity,
                                   _fixed_after(d, name, version), f"https://osv.dev/vulnerability/{vid}"))
    return signals


def scan(ctx: Context) -> list[Signal] | None:
    if ctx.repo and ctx.gh:
        try:
            return from_dependabot(ctx)
        except GitHubError as e:
            if e.status not in (403, 404):
                raise
            ctx.warnings.append(f"advisory: Dependabot alerts not readable ({e.status}); using OSV.dev instead")
    if not ctx.path:
        return None
    return from_osv(ctx)
