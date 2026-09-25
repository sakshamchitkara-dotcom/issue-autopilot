"""Core data types shared by sources, triage and the CLI."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# Priority order: lower number = more urgent.
PRIORITIES = {"P1": 1, "P2": 2, "P3": 3}


@dataclass
class Signal:
    """One thing a source noticed, e.g. a single TODO line or one stale package."""

    kind: str  # source name: todo | ci | deps | stale-pr | secret
    group: str  # signals with the same (kind, group) become one issue
    summary: str  # one-line human description
    priority: str = "P3"
    path: str | None = None
    line: int | None = None
    detail: str = ""  # longer text (log excerpt, blame info, ...)
    meta: dict = field(default_factory=dict)


@dataclass
class Issue:
    """A group of signals rendered into one GitHub issue."""

    kind: str
    group: str
    signals: list[Signal]
    title: str = ""
    body: str = ""
    labels: list[str] = field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.kind, self.group)

    @property
    def priority(self) -> str:
        return min((s.priority for s in self.signals), key=PRIORITIES.__getitem__)


def fingerprint(kind: str, group: str) -> str:
    return hashlib.sha256(f"{kind}\0{group}".encode()).hexdigest()[:16]
