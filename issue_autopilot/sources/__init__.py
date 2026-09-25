"""Signal sources. Each source is `scan(ctx) -> list[Signal] | None`, registered in `_registry()`."""
from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Iterator

from ..github import GitHub
from ..models import Signal

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox", "vendor"}
MAX_FILE_BYTES = 1_000_000


@dataclass
class Context:
    path: str | None  # local checkout (for file-based sources)
    repo: str | None  # owner/repo (for API-based sources)
    gh: GitHub | None
    options: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def iter_text_files(root: str, exclude: list[str] | None = None,
                    max_bytes: int = MAX_FILE_BYTES) -> Iterator[tuple[str, str]]:
    """Yield (relative_path, text) for tracked (or, outside git, all) text files.

    `exclude` holds fnmatch globs against the relative path, e.g. "tests/*".
    """
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, timeout=30)
        rels = [p for p in out.stdout.decode().split("\0") if p] if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        rels = None
    if rels is None:
        rels = []
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            rels += [os.path.relpath(os.path.join(d, f), root) for f in files]
    for rel in sorted(rels):
        if set(rel.split(os.sep)[:-1]) & SKIP_DIRS:
            continue
        if exclude and any(fnmatch.fnmatch(rel, pat) for pat in exclude):
            continue
        full = os.path.join(root, rel)
        try:
            if not os.path.isfile(full) or os.path.getsize(full) > max_bytes:
                continue
            with open(full, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if b"\0" in raw[:8192]:  # binary
            continue
        yield rel, raw.decode("utf-8", errors="replace")


def _registry() -> dict[str, Callable[[Context], list[Signal]]]:
    from . import action_versions, actions, advisories, deps, flaky, prs, secrets, todos

    return {
        "todo": todos.scan,
        "secret": secrets.scan,
        "deps": deps.scan,
        "advisory": advisories.scan,
        "ci": actions.scan,
        "actions": action_versions.scan,
        "flaky": flaky.scan,
        "stale-pr": prs.scan,
    }


def run_sources(ctx: Context, names: list[str] | None = None) -> tuple[list[Signal], list[str]]:
    """Run the selected sources. Returns (signals, names_that_ran_ok).

    A source that errors is reported as a warning and excluded from the "ran ok" list,
    so close-resolved never closes issues just because a source failed.
    """
    registry = _registry()
    signals: list[Signal] = []
    ok: list[str] = []
    for name in names or list(registry):
        if name not in registry:
            raise SystemExit(f"unknown source: {name} (choose from {', '.join(registry)})")
        try:
            found = registry[name](ctx)
        except Exception as e:  # one broken source must not sink the run
            ctx.warnings.append(f"source {name} failed: {e}")
            continue
        if found is None:  # source not applicable (e.g. no repo for API sources)
            continue
        signals += found
        ok.append(name)
    return signals, ok
