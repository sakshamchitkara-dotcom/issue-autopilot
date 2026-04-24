"""Dependency staleness: version specs in requirements*.txt / pyproject.toml / package.json vs registry latest."""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from ..github import http_get_json
from ..models import Signal
from . import Context, iter_text_files

# name[extras] <spec> ; markers  -- lines without a version spec (or `name @ url`) are skipped
REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*((?:===?|~=|>=|<=|!=|<|>)[^;#]*)")
NPM_SPEC = re.compile(r"^(?:[\^~]|[<>]=?|=)?\s*v?\d")
CLAUSE = re.compile(r"^(===|==|~=|>=|<=|!=|<|>|\^|~|=)?v?(\d+(?:\.(?:\d+|\*|x))*)")


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0].split("+")[0])[:4])


def _pad(t: tuple[int, ...]) -> tuple[int, ...]:
    return t + (0,) * (4 - len(t))


def _bump(t: tuple[int, ...], idx: int) -> tuple[int, ...]:
    """Smallest version above every release sharing t[:idx+1], e.g. (1, 4, 2) @1 -> (1, 5)."""
    return t[:idx] + (t[idx] + 1,)


def bounds(spec: str) -> tuple[tuple, tuple | None, bool, bool] | None:
    """Parse a PEP 440 / npm / poetry spec into (floor, upper, upper_inclusive, exact).

    Handles ==, ===, bare pins, wildcards (1.2.* / 1.x), >=, >, <, <=, ~=, ^ and ~, joined by
    commas or spaces. Returns None when there is nothing usable (e.g. "||" alternatives).
    """
    # ponytail: no support for npm "||" or hyphen ranges; those specs are skipped, not guessed.
    if "||" in spec or " - " in spec:
        return None
    spec = re.sub(r"(===|==|~=|>=|<=|!=|<|>|\^|~|=)\s+", r"\1", spec.strip())
    floor, upper, inclusive, exact = None, None, False, False
    for clause in filter(None, re.split(r"[,\s]+", spec)):
        m = CLAUSE.match(clause)
        if not m:
            return None
        op, raw = m.group(1) or "==", m.group(2)
        parts = raw.split(".")
        wild = next((i for i, x in enumerate(parts) if x in ("*", "x")), None)
        if wild is not None:
            parts = parts[:wild]
        v = tuple(int(x) for x in parts[:4])
        if op == "!=" or not v and wild is None:
            continue
        if op in ("==", "===", "=") and wild is not None:
            floor, upper = v, (_bump(v, len(v) - 1) if v else None)
        elif op in ("==", "===", "="):
            floor, exact = v, True
        elif op in (">=", ">"):
            floor = v
        elif op == "<":
            upper = v
        elif op == "<=":
            upper, inclusive = v, True
        elif op == "~=":
            if len(v) < 2:
                return None
            floor, upper = v, _bump(v, len(v) - 2)
        elif op == "^":
            nz = next((i for i, x in enumerate(v) if x), len(v) - 1)
            floor, upper = v, _bump(v, nz)
        elif op == "~":
            floor, upper = v, _bump(v, 0 if len(v) == 1 else 1)
    if floor is None:
        return None
    return floor, upper, inclusive, exact


def behind(spec: str, latest: str) -> bool:
    """True when `latest` is outside what `spec` allows (pins: newer than the pin)."""
    b, new = bounds(spec), _pad(version_tuple(latest))
    if not b or not version_tuple(latest):
        return False
    floor, upper, inclusive, exact = b
    if exact:
        return new > _pad(floor)
    if upper is None:
        return False
    return new > _pad(upper) if inclusive else new >= _pad(upper)


def majors_behind(spec: str, latest: str) -> int:
    """How many major versions `latest` is ahead of the spec's floor (0 when unknown)."""
    b, new = bounds(spec), version_tuple(latest)
    return max(0, new[0] - b[0][0]) if b and new else 0


def pypi_latest(name: str) -> str:
    return http_get_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")["info"]["version"]


def npm_latest(name: str) -> str:
    return http_get_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@/')}/latest")["version"]


def parse_requirements(text: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2).strip()) for line in text.splitlines() if (m := REQ_LINE.match(line))]


def parse_package_json(text: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    out = []
    for section in ("dependencies", "devDependencies"):
        for name, spec in (data.get(section) or {}).items():
            spec = str(spec).strip()
            if NPM_SPEC.match(spec):  # skip "*", "latest", git urls, workspace: etc.
                out.append((name, spec))
    return out


def parse_pyproject(text: str) -> list[tuple[str, str]]:
    """[project] dependencies (PEP 508 strings) plus poetry dependency tables, incl. groups."""
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    out = []
    for req in (data.get("project") or {}).get("dependencies") or []:
        if m := REQ_LINE.match(str(req)):
            out.append((m.group(1), m.group(2).strip()))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
    tables += [g.get("dependencies") for g in (poetry.get("group") or {}).values() if isinstance(g, dict)]
    for table in filter(None, tables):
        for name, spec in table.items():
            if isinstance(spec, dict):
                spec = spec.get("version")
            if name.lower() != "python" and isinstance(spec, str) and bounds(spec):
                out.append((name, spec.strip()))
    return out


def manifest_deps(ctx: Context) -> list[tuple[str, str, str, str]]:
    """(manifest, ecosystem, name, spec) for every comparable dependency in the checkout."""
    wanted = []
    for rel, text in iter_text_files(ctx.path, ctx.options.get("exclude")):
        base = rel.rsplit("/", 1)[-1]
        if re.fullmatch(r"requirements[\w.-]*\.txt", base):
            wanted += [(rel, "pypi", n, v) for n, v in parse_requirements(text)]
        elif base == "package.json":
            wanted += [(rel, "npm", n, v) for n, v in parse_package_json(text)]
        elif base == "pyproject.toml":
            wanted += [(rel, "pypi", n, v) for n, v in parse_pyproject(text)]
    return [w for w in wanted if bounds(w[3])]  # nothing to compare for "*", "||" etc.


def scan(ctx: Context) -> list[Signal] | None:
    if not ctx.path:
        return None
    wanted = manifest_deps(ctx)

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
    for (manifest, eco, name, spec), newest in zip(wanted, latest):
        if not newest:
            continue
        lag = majors_behind(spec, newest)
        blocked = behind(spec, newest)
        if not blocked and not lag:  # in range, or an unbounded floor less than a major behind
            continue
        shown = spec.lstrip("=") if re.fullmatch(r"==?\d[\w.]*", spec) else spec
        note = f" ({lag} major{'s' * (lag != 1)} behind)" if lag else ""
        if not blocked:
            note = f" (allowed, but the floor is {lag} major{'s' * (lag != 1)} behind)"
        signals.append(
            Signal(
                kind="deps",
                group=manifest,
                summary=f"{name} {shown} → {newest}{note}",
                priority="P2" if blocked and lag else "P3",
                path=manifest,
                meta={"ecosystem": eco, "package": name, "spec": spec, "latest": newest, "majors_behind": lag},
            )
        )
    return signals
