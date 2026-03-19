"""TODO / FIXME / HACK / XXX comments, one issue per file."""
from __future__ import annotations

import re

from ..models import Signal
from . import Context, iter_text_files
from .blame import annotate

# Tag must follow a comment marker so identifiers like TODO_LIST don't match.
PATTERN = re.compile(
    r"(?:#|//|/\*|<!--|--|;|^\s*\*)\s*(TODO|FIXME|HACK|XXX)\b(?:\(([^)]*)\))?[:\s]*(.*)"
)
URGENT = {"FIXME", "HACK", "XXX"}


def scan(ctx: Context) -> list[Signal] | None:
    if not ctx.path:
        return None
    signals = []
    for rel, text in iter_text_files(ctx.path, ctx.options.get("exclude")):
        for n, line in enumerate(text.splitlines(), 1):
            m = PATTERN.search(line)
            if not m:
                continue
            tag, owner, msg = m.group(1), m.group(2), m.group(3).strip().rstrip("*/->").strip()
            signals.append(
                Signal(
                    kind="todo",
                    group=rel,
                    summary=f"{tag}: {msg or '(no description)'}",
                    priority="P2" if tag in URGENT else "P3",
                    path=rel,
                    line=n,
                    meta={"tag": tag, "owner": owner},
                )
            )
    annotate(ctx.path, signals)
    return signals
