"""Grouping, labels, priority and dedupe fingerprints."""
from __future__ import annotations

import re
from collections import defaultdict

from .models import PRIORITIES, Issue, Signal

BOT_LABEL = "autopilot"
KIND_LABELS = {
    "todo": "tech-debt",
    "ci": "ci",
    "deps": "dependencies",
    "stale-pr": "stale",
    "secret": "security",
}
# sig= was added in 0.2; v0.1 markers without it still parse (digest None).
MARKER_RX = re.compile(r"<!--\s*issue-autopilot\s+fp=([0-9a-f]{16})\s+kind=([\w-]+)(?:\s+sig=([0-9a-f]{12}))?\s*-->")


def marker(issue: Issue) -> str:
    return f"<!-- issue-autopilot fp={issue.fingerprint} kind={issue.kind} sig={issue.digest} -->"


def parse_marker(body: str | None) -> tuple[str, str] | None:
    m = MARKER_RX.search(body or "")
    return (m.group(1), m.group(2)) if m else None


def marker_digest(body: str | None) -> str | None:
    m = MARKER_RX.search(body or "")
    return m.group(3) if m else None


def group(signals: list[Signal]) -> list[Issue]:
    """Merge signals sharing (kind, group) into one Issue; most urgent first."""
    buckets: dict[tuple[str, str], list[Signal]] = defaultdict(list)
    for s in signals:
        buckets[(s.kind, s.group)].append(s)
    issues = []
    for (kind, grp), sigs in buckets.items():
        sigs.sort(key=lambda s: (PRIORITIES[s.priority], s.line or 0))
        issue = Issue(kind=kind, group=grp, signals=sigs)
        issue.labels = [BOT_LABEL, KIND_LABELS.get(kind, kind), issue.priority]
        issues.append(issue)
    issues.sort(key=lambda i: (PRIORITIES[i.priority], i.kind, i.group))
    return issues


def index_existing(open_issues: list[dict]) -> dict[str, dict]:
    """fingerprint -> GitHub issue, for issues this tool filed earlier."""
    out = {}
    for gi in open_issues:
        parsed = parse_marker(gi.get("body"))
        if parsed:
            out[parsed[0]] = gi
    return out


def plan(issues: list[Issue], existing: dict[str, dict], cap: int) -> tuple[list[Issue], list[Issue], list[Issue]]:
    """Split into (to_create, duplicates, over_cap)."""
    new = [i for i in issues if i.fingerprint not in existing]
    dupes = [i for i in issues if i.fingerprint in existing]
    return new[:cap], dupes, new[cap:]


def resolved(existing: dict[str, dict], current: list[Issue], scanned_kinds: list[str]) -> list[dict]:
    """Open bot issues whose fingerprint vanished, limited to kinds that were actually scanned."""
    live = {i.fingerprint for i in current}
    out = []
    for fp, gi in existing.items():
        _, kind = parse_marker(gi.get("body"))
        if kind in scanned_kinds and fp not in live:
            out.append(gi)
    return out
