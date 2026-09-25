"""Dependency staleness: pinned versions in requirements*.txt / package.json vs registry latest."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from ..github import http_get_json
from ..models import Signal
from . import Context, iter_text_files

REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")
NPM_VERSION = re.compile(r"^[\^~]?(\d+(?:\.\d+)*)")


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0].split("+")[0])[:4])


def pypi_latest(name: str) -> str:
    return http_get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")["info"]["version"]


def npm_latest(name: str) -> str:
    return http_get_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@/')}/latest")["version"]


def parse_requirements(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for line in text.splitlines() if (m := REQ_LINE.match(line))]


def parse_package_json(text: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for section in ("dependencies", "devDependencies"):
        for name, spec in (data.get(section) or {}).items():
            m = NPM_VERSION.match(str(spec).strip())
            if m:  # skip "*", "latest", git urls, workspace: etc.
                out.append((name, m.group(1)))
    return out


def scan(ctx: Context) -> list[Signal] | None:
    if not ctx.path:
        return None
    wanted = []  # (manifest, ecosystem, name, current)
    for rel, text in iter_text_files(ctx.path):
        base = rel.rsplit("/", 1)[-1]
        if re.fullmatch(r"requirements[\w.-]*\.txt", base):
            wanted += [(rel, "pypi", n, v) for n, v in parse_requirements(text)]
        elif base == "package.json":
            wanted += [(rel, "npm", n, v) for n, v in parse_package_json(text)]

    errors: list[str] = []

    def lookup(item):
        _, eco, name, _ = item
        try:
            return (pypi_latest if eco == "pypi" else npm_latest)(name)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # private / unpublished package: nothing to compare against
                ctx.warnings.append(f"deps: {eco}:{name} not found on registry, skipped")
                return None
            errors.append(f"{eco}:{name}: {e}")
        except Exception as e:
            errors.append(f"{eco}:{name}: {e}")
        return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        latest = list(pool.map(lookup, wanted))
    if errors:
        # Fail the whole source: partial results could make close-resolved close live issues.
        raise RuntimeError(f"{len(errors)} registry lookup(s) failed, e.g. {errors[0]}")

    signals = []
    for (manifest, eco, name, current), newest in zip(wanted, latest):
        if not newest:
            continue
        cur, new = version_tuple(current), version_tuple(newest)
        if not cur or not new or new <= cur:
            continue
        major = new[0] > cur[0]
        signals.append(
            Signal(
                kind="deps",
                group=manifest,
                summary=f"{name} {current} → {newest}" + (" (major)" if major else ""),
                priority="P2" if major else "P3",
                path=manifest,
                meta={"ecosystem": eco, "package": name, "current": current, "latest": newest},
            )
        )
    return signals
