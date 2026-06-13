"""Pick an owner for an issue: CODEOWNERS first, then whoever last touched the flagged lines."""
from __future__ import annotations

import os
import re
import subprocess
from collections import Counter

from .github import GitHubError
from .models import Issue

CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
NOREPLY = re.compile(r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$")
MAX_BLAME = 20


def load_codeowners(root: str) -> list[tuple[str, list[str]]]:
    for rel in CODEOWNERS_PATHS:
        try:
            with open(os.path.join(root, rel)) as fh:
                text = fh.read()
        except OSError:
            continue
        rules = []
        for line in text.splitlines():
            parts = line.split("#", 1)[0].split()
            if parts:
                rules.append((parts[0], parts[1:]))
        return rules
    return []


def _pattern_rx(pattern: str) -> re.Pattern:
    """CODEOWNERS (gitignore-style) glob -> regex: `*`/`?` stay inside one path segment, `**` crosses.

    A slash at the start or in the middle anchors the pattern to the repo root; otherwise it
    matches at any depth. A match on a directory covers everything under it, except `dir/*`,
    which GitHub documents as matching only direct children."""
    anchored = "/" in pattern.rstrip("/")
    pat = pattern.strip("/")
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            out.append({"*": "[^/]*", "?": "[^/]"}.get(pat[i], re.escape(pat[i])))
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "" if pat.endswith("/*") else "(?:/.*)?"
    return re.compile(prefix + "".join(out) + suffix)


def matches(pattern: str, path: str) -> bool:
    return _pattern_rx(pattern).fullmatch(path) is not None


def codeowner(rules: list[tuple[str, list[str]]], path: str) -> str | None:
    """First individual @user of the last matching rule (GitHub semantics); teams and emails are skipped."""
    for pattern, owners in reversed(rules):
        if matches(pattern, path):
            users = [o[1:] for o in owners if o.startswith("@") and "/" not in o]
            return users[0] if users else None
    return None


def _last_commit(root: str, path: str, line: int | None) -> tuple[str, str] | None:
    """(sha, author email) of the commit that last touched `path` (or that one line)."""
    cmd = (["git", "-C", root, "blame", "--porcelain", "-L", f"{line},{line}", "--", path] if line else
           ["git", "-C", root, "log", "-1", "--format=%H%n<%ae>", "--", path])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    sha = out.stdout.split(None, 1)[0]
    mail = re.search(r"^(?:author-mail )?<([^>]*)>", out.stdout, re.M)
    return (sha, mail.group(1)) if len(sha) == 40 and mail else None


def blame_owner(root: str, issue: Issue, gh=None, repo: str | None = None) -> str | None:
    commits = Counter(c for s in issue.signals[:MAX_BLAME] if s.path and (c := _last_commit(root, s.path, s.line)))
    if not commits:
        return None
    (sha, mail), _ = commits.most_common(1)[0]
    if m := NOREPLY.match(mail):
        return m.group(1)
    if gh and repo:  # the API maps the commit to an account even for private emails
        try:
            return ((gh.get(f"/repos/{repo}/commits/{sha}") or {}).get("author") or {}).get("login")
        except GitHubError:
            return None
    return None


def owner_for(root: str | None, issue: Issue, rules, gh=None, repo: str | None = None) -> tuple[str, str] | None:
    """(login, how) for the issue, or None when nobody can be determined."""
    paths = Counter(s.path for s in issue.signals if s.path)
    if not (root and paths):
        return None
    top = paths.most_common(1)[0][0]
    if login := codeowner(rules, top):
        return login, "CODEOWNERS"
    if login := blame_owner(root, issue, gh, repo):
        return login, "git blame"
    return None
