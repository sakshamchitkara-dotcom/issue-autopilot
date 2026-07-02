"""Grouping, labels, priority and dedupe fingerprints."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from .models import PRIORITIES, Issue, Signal

BOT_LABEL = "autopilot"
RESOLVED_LABEL = "autopilot:resolved"  # added by close-resolved; a closed issue without it was closed by a human
KIND_LABELS = {
    "todo": "tech-debt",
    "ci": "ci",
    "deps": "dependencies",
    "stale-pr": "stale",
    "secret": "security",
    "advisory": "security",
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


def is_open(gi: dict) -> bool:
    return gi.get("state", "open") == "open"


def closed_by_human(gi: dict) -> bool:
    return not is_open(gi) and RESOLVED_LABEL not in {lb["name"] for lb in gi.get("labels", [])}


def reopened_since_resolved(events: list[dict]) -> bool:
    """True when an issue was reopened after close-resolved labelled it: whatever closed it last was a human."""
    labelled = [i for i, e in enumerate(events)
                if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == RESOLVED_LABEL]
    return bool(labelled) and any(e.get("event") == "reopened" for e in events[labelled[-1]:])


def index_existing(issues: list[dict]) -> dict[str, dict]:
    """fingerprint -> GitHub issue this tool filed earlier. Open beats closed, then newest wins."""
    out: dict[str, dict] = {}
    for gi in issues:
        parsed = parse_marker(gi.get("body"))
        if not parsed:
            continue
        cur = out.get(parsed[0])
        if cur is None or (is_open(gi), gi.get("number", 0)) > (is_open(cur), cur.get("number", 0)):
            out[parsed[0]] = gi
    return out


@dataclass
class Plan:
    create: list[Issue] = field(default_factory=list)
    update: list[Issue] = field(default_factory=list)  # open, but the signals changed
    unchanged: list[Issue] = field(default_factory=list)
    reopen: list[Issue] = field(default_factory=list)  # closed by a human, --reopen given
    suppressed: list[Issue] = field(default_factory=list)  # closed by a human: respect it
    over: list[Issue] = field(default_factory=list)  # deferred by the per-run cap


def plan(issues: list[Issue], existing: dict[str, dict], cap: int, reopen: bool = False) -> Plan:
    """Decide what to do with each issue group.

    New issues and reopens share the cap; edits have their own. A group whose issue was
    closed by close-resolved and came back is filed fresh (a regression); one a human
    closed is left alone unless `reopen`.
    """
    p = Plan()
    for i in issues:
        gi = existing.get(i.fingerprint)
        if gi is None or (not is_open(gi) and not closed_by_human(gi)):
            p.create.append(i)
        elif closed_by_human(gi):
            (p.reopen if reopen else p.suppressed).append(i)
        elif marker_digest(gi.get("body")) != i.digest:
            p.update.append(i)
        else:
            p.unchanged.append(i)
    new = p.reopen + p.create
    p.over = new[cap:] + p.update[cap:]
    keep = {id(i) for i in new[:cap]}
    p.reopen = [i for i in p.reopen if id(i) in keep]
    p.create = [i for i in p.create if id(i) in keep]
    p.update = p.update[:cap]
    return p


def resolved(existing: dict[str, dict], current: list[Issue], scanned_kinds: list[str]) -> list[dict]:
    """Open bot issues whose fingerprint vanished, limited to kinds that were actually scanned."""
    live = {i.fingerprint for i in current}
    out = []
    for fp, gi in existing.items():
        if not is_open(gi):
            continue
        _, kind = parse_marker(gi.get("body"))
        if kind in scanned_kinds and fp not in live:
            out.append(gi)
    return out
