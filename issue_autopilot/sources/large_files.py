"""Big binary files committed to the repo (they bloat every clone forever). One issue per repo."""
from __future__ import annotations

import os

from ..models import Signal
from . import Context, list_files

DEFAULT_MB = 5
GITHUB_WARN_MB = 50  # GitHub warns on push above this and rejects files over 100 MB


def scan(ctx: Context) -> list[Signal] | None:
    if not ctx.path:
        return None
    limit = float(ctx.options.get("large_file_mb") or DEFAULT_MB) * 1024 * 1024
    out = []
    for rel in list_files(ctx.path, ctx.options.get("exclude")):
        full = os.path.join(ctx.path, rel)
        try:
            size = os.path.getsize(full)
            if size < limit or not os.path.isfile(full):
                continue
            with open(full, "rb") as fh:
                if b"\0" not in fh.read(8192):  # big text (lockfiles, fixtures) diffs fine; LFS pointers are tiny
                    continue
        except OSError:
            continue
        mb = size / 1024 / 1024
        out.append(Signal("large-file", "repository", f"{mb:.1f} MB binary file", "P2" if mb >= GITHUB_WARN_MB else "P3",
                          path=rel, meta={"bytes": size}))
    return sorted(out, key=lambda s: -s.meta["bytes"])
