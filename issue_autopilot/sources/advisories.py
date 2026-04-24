"""Known vulnerabilities: GitHub Dependabot alerts when readable, else OSV.dev for exact pins."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from ..github import GitHubError, http_get_json, http_post_json
from ..models import Signal
from . import Context
from .deps import bounds, manifest_deps, version_tuple

OSV = "https://api.osv.dev/v1"
OSV_ECOSYSTEM = {"pypi": "PyPI", "npm": "npm"}
URGENT = {"critical", "high"}


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


def from_osv(ctx: Context) -> list[Signal]:
    pins = []  # (manifest, eco, name, version) -- OSV needs one concrete version, so ranges are skipped
    for manifest, eco, name, spec in manifest_deps(ctx):
        if eco in OSV_ECOSYSTEM and bounds(spec)[3]:
            pins.append((manifest, eco, name, re.sub(r"^=+", "", spec).strip()))
    if not pins:
        return []
    queries = [{"package": {"name": n, "ecosystem": OSV_ECOSYSTEM[e]}, "version": v} for _, e, n, v in pins]
    results = http_post_json(f"{OSV}/querybatch", {"queries": queries})["results"]
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
