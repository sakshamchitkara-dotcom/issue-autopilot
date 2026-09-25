"""Repository files GitHub's community profile expects: a LICENSE and a security policy. One issue per repo."""
from __future__ import annotations

import re

from ..github import GitHubError
from ..models import Signal
from . import Context, list_files

LICENSE = re.compile(r"(?i)^(licen[cs]e|copying)([.-].*)?$")  # LICENSE, LICENSE.md, LICENSE-MIT, COPYING
SECURITY = re.compile(r"(?i)^(\.github/|docs/)?security\.(md|rst|txt)$")  # the places GitHub looks


def is_private(ctx: Context) -> bool:
    if not (ctx.gh and ctx.repo):
        return False
    try:
        return bool(ctx.gh.get(f"/repos/{ctx.repo}").get("private"))
    except GitHubError:
        return False  # can't tell: say it, a missing LICENSE is cheap to dismiss


def scan(ctx: Context) -> list[Signal] | None:
    if not ctx.path:
        return None
    files = list_files(ctx.path)
    out = []
    if not any(LICENSE.match(f) for f in files) and not is_private(ctx):
        out.append(Signal("hygiene", "repository", "No LICENSE file: without one, nobody may legally reuse "
                          "this code", "P2", meta={"file": "LICENSE"}))
    if not any(SECURITY.match(f) for f in files):
        out.append(Signal("hygiene", "repository", "No SECURITY.md: reporters have no private way to disclose "
                          "a vulnerability", "P3", meta={"file": "SECURITY.md"}))
    return out
