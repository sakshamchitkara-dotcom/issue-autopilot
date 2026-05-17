"""Deterministic issue titles/bodies. The Claude summarizer only replaces title + intro prose."""
from __future__ import annotations

import re

from .models import Issue, Signal
from .triage import marker

MAX_ITEMS = 50
MAX_BODY = 60_000  # GitHub's hard limit is 65,536 chars
MENTION = re.compile(r"(?<![\w`/])@(?=[A-Za-z0-9])")
AGE = re.compile(r"\d+d old")
RUN_LINK = re.compile(r"\[run\]\([^)]*\) @ \w*")
LINE_NO = re.compile(r"(`[^`\s]+):\d+`")


def no_pings(text: str) -> str:
    """Break @mentions with a zero-width space so filed issues never notify anyone."""
    return MENTION.sub("@​", text)


def title_for(issue: Issue) -> str:
    n, g = len(issue.signals), issue.group
    if issue.kind == "todo":
        tags = sorted({s.meta.get("tag", "TODO") for s in issue.signals})
        return f"Tech debt: {n} {'/'.join(tags)} comment{'s' * (n != 1)} in {g}"
    if issue.kind == "secret":
        return f"Security: possible hardcoded secret{'s' * (n != 1)} in {g}"
    if issue.kind == "advisory":
        return f"Security: {n} known vulnerabilit{'ies' if n != 1 else 'y'} in dependencies from {g}"
    if issue.kind == "deps":
        return f"Dependencies: {n} outdated package{'s' * (n != 1)} in {g}"
    if issue.kind == "ci":
        return issue.signals[0].summary.replace("Workflow", "CI failing: workflow", 1)
    if issue.kind == "stale-pr":
        return f"Stale PRs: {n} open pull request{'s' * (n != 1)} with no recent activity"
    return f"{issue.kind}: {n} signal{'s' * (n != 1)} in {g}"


def intro_for(issue: Issue) -> str:
    return {
        "todo": "These comments mark known shortcuts or unfinished work. Resolve them or convert them into tracked tasks.",
        "secret": "Lines below match patterns for credentials. If any are real: **rotate the credential first**, then remove it from the code and history.",
        "advisory": "These dependency versions have published security advisories. Upgrade to a fixed release; "
                    "if none exists, check whether the vulnerable code path is used.",
        "deps": "These dependency specs don't allow the latest release on their registry, or their floor is a major "
                "version or more behind it. Review changelogs and upgrade.",
        "ci": "The latest run of this workflow on the default branch failed. Excerpt of the failing job log is below.",
        "stale-pr": "These pull requests have had no activity for a while. Merge, close, or ping for review.",
    }.get(issue.kind, "Automatically detected signals.")


def _line(s: Signal) -> str:
    loc = f"`{s.path}:{s.line}`" if s.path and s.line else (f"`{s.path}`" if s.path else "")
    extra = []
    if "author" in s.meta:
        extra.append(f"{s.meta['author']}, {s.meta.get('age_days', 0)}d old")
    if s.meta.get("url"):
        extra.append(s.meta["url"])
    if s.meta.get("run_url"):
        extra.append(f"[run]({s.meta['run_url']}) @ {s.meta.get('sha', '')}")
    tail = f" ({'; '.join(extra)})" if extra else ""
    return f"- [{s.priority}] {loc} {s.summary}{tail}".replace("  ", " ")


def signals_md(issue: Issue) -> str:
    lines = [_line(s) for s in issue.signals[:MAX_ITEMS]]
    if len(issue.signals) > MAX_ITEMS:
        lines.append(f"- …and {len(issue.signals) - MAX_ITEMS} more")
    out = "### Signals\n" + "\n".join(lines)
    details = [s.detail for s in issue.signals if s.detail]
    if details:
        out += "\n\n### Details\n" + "\n\n".join(f"````text\n{d}\n````" for d in details)
    return out


def footer(issue: Issue) -> str:
    return (
        "---\n_Filed by [issue-autopilot](https://github.com/sakshamchitkara-dotcom/issue-autopilot). "
        "It will be closed automatically by `autopilot close-resolved` once these signals disappear._\n"
        + marker(issue)
    )


def render(issue: Issue, title: str | None = None, intro: str | None = None) -> Issue:
    issue.title = no_pings((title or title_for(issue)).strip())[:250]
    body = f"{intro or intro_for(issue)}\n\n**Priority:** {issue.priority}\n\n{signals_md(issue)}"
    body = no_pings(body)
    tail = "\n\n" + footer(issue)  # marker must survive truncation
    issue.body = body[: MAX_BODY - len(tail)] + tail
    return issue


def changelog(old_body: str | None, new_body: str) -> str:
    """Comment listing signal bullets added/removed between two bodies; "" when nothing visible changed.

    Line moves, blame age and CI run links are ignored."""
    def items(body):
        return {LINE_NO.sub(r"\1`", RUN_LINK.sub("", AGE.sub("", ln))): ln
                for ln in (body or "").splitlines() if ln.startswith("- [")}

    old, new = items(old_body), items(new_body)
    added = [new[k] for k in new if k not in old]
    removed = [old[k] for k in old if k not in new]
    out = ["issue-autopilot updated this issue because its signals changed."]
    if added:
        out += ["", "**Added**", *added]
    if removed:
        out += ["", "**Removed**", *removed]
    if not (added or removed):  # only line numbers or formatting moved: not worth a notification
        return ""
    return no_pings("\n".join(out))
