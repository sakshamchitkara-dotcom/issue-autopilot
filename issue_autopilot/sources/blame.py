"""Enrich line-level signals with git blame author and age."""
from __future__ import annotations

import subprocess
import time
from collections import defaultdict

from ..models import Signal

OLD_DAYS = 365


def blame_file(root: str, rel: str) -> dict[int, tuple[str, int]]:
    """Map final line number -> (author, unix time) using one porcelain blame per file."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "blame", "--line-porcelain", "--", rel],
            capture_output=True, text=True, timeout=60, errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if out.returncode != 0:
        return {}
    result: dict[int, tuple[str, int]] = {}
    line_no, author, ts = 0, "", 0
    for row in out.stdout.splitlines():
        if row.startswith("\t"):  # content line closes a record
            result[line_no] = (author, ts)
        elif row.startswith("author "):
            author = row[7:]
        elif row.startswith("author-time "):
            ts = int(row[12:])
        else:
            parts = row.split(" ")
            if len(parts) >= 3 and len(parts[0]) == 40:
                line_no = int(parts[2])
    return result


def annotate(root: str, signals: list[Signal], now: float | None = None) -> None:
    now = now or time.time()
    by_file = defaultdict(list)
    for s in signals:
        if s.path and s.line:
            by_file[s.path].append(s)
    for rel, sigs in by_file.items():
        info = blame_file(root, rel)
        for s in sigs:
            if s.line not in info:
                continue
            author, ts = info[s.line]
            age = int((now - ts) // 86400) if ts else 0
            s.meta.update(author=author, age_days=age)
            # Ancient TODOs get bumped: they're either real debt or dead comments.
            if age >= OLD_DAYS and s.priority == "P3":
                s.priority = "P2"
