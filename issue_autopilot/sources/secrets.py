"""Hardcoded-secret patterns. Matches are always redacted before leaving this module."""
from __future__ import annotations

import re

from ..models import Signal
from . import Context, iter_text_files

RULES = [
    ("aws-access-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    (
        "generic-secret",
        re.compile(
            r"""(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|passw(?:or)?d|access[_-]?token|auth[_-]?token)\b"""
            r"""\s*[:=]\s*["']([^"'\s]{12,})["']"""
        ),
    ),
]
PLACEHOLDER = re.compile(r"(?i)example|dummy|placeholder|changeme|your[_-]|xxxx|\$\{|<[^>]*>|\*\*\*")
SKIP_FILES = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|poetry\.lock|pnpm-lock\.yaml)$")


def redact(value: str) -> str:
    return value[:4] + "…" + f"({len(value)} chars)" if len(value) > 8 else "…"


def scan(ctx: Context) -> list[Signal] | None:
    if not ctx.path:
        return None
    signals = []
    for rel, text in iter_text_files(ctx.path):
        if SKIP_FILES.search(rel):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for rule, rx in RULES:
                m = rx.search(line)
                if not m:
                    continue
                value = m.group(m.lastindex or 0) if rule == "generic-secret" else m.group(0)
                if PLACEHOLDER.search(value):
                    continue
                signals.append(
                    Signal(
                        kind="secret",
                        group=rel,
                        summary=f"possible {rule} ({redact(value)})",
                        priority="P1",
                        path=rel,
                        line=n,
                        meta={"rule": rule},
                    )
                )
                break  # one finding per line is enough
    return signals
