"""GitHub Actions in workflow files pinned to an older major version than the action's latest release."""
from __future__ import annotations

import re

from ..github import GitHub, GitHubError
from ..models import Signal
from . import Context, iter_text_files
from .deps import version_tuple

WORKFLOW = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
# `uses: owner/repo[/path]@ref`; local (./) and docker:// actions have no owner/repo@ref form.
USES = re.compile(r"""^\s*-?\s*uses:\s*["']?([\w.-]+)/([\w.-]+)(?:/[^@\s"']*)?@([^\s"'#]+)""")
VERSION_REF = re.compile(r"^v?\d+(?:\.\d+){0,2}$")  # v4, v4.1, 4.1.2; SHAs and branches are skipped


def latest_version(gh: GitHub, slug: str) -> str | None:
    """Tag of the latest release, else the highest version-looking tag; None if the action repo is gone."""
    try:
        return gh.get(f"/repos/{slug}/releases/latest")["tag_name"]
    except GitHubError as e:
        if e.status != 404:
            raise
    try:
        tags = [t["name"] for t in gh.get(f"/repos/{slug}/tags?per_page=100") if VERSION_REF.match(t["name"])]
    except GitHubError as e:
        if e.status == 404:
            return None
        raise
    return max(tags, key=version_tuple, default=None)


def scan(ctx: Context) -> list[Signal] | None:
    if not ctx.path:
        return None
    uses = []  # (workflow, line, slug, ref)
    for rel, text in iter_text_files(ctx.path, ctx.options.get("exclude")):
        if WORKFLOW.match(rel):
            for n, line in enumerate(text.splitlines(), 1):
                if (m := USES.match(line)) and VERSION_REF.match(m.group(3)):
                    uses.append((rel, n, f"{m.group(1)}/{m.group(2)}", m.group(3)))
    if not uses:
        return []
    gh = ctx.gh or GitHub(None)  # unauthenticated works for public actions, with a low rate limit
    latest: dict[str, str | None] = {}
    signals = []
    for workflow, line, slug, ref in uses:
        if slug not in latest:  # errors other than 404 propagate: a partial scan must not close issues
            latest[slug] = latest_version(gh, slug)
            if latest[slug] is None:
                ctx.warnings.append(f"actions: {slug} has no releases or version tags; skipped")
        newest = latest[slug]
        if not newest or not version_tuple(newest) or not version_tuple(ref):
            continue
        lag = version_tuple(newest)[0] - version_tuple(ref)[0]
        if lag < 1:
            continue
        signals.append(Signal(
            kind="actions",
            group=workflow,
            summary=f"{slug}@{ref} → {newest} ({lag} major{'s' * (lag != 1)} behind)",
            priority="P2" if lag >= 2 else "P3",
            path=workflow,
            line=line,
            meta={"action": slug, "ref": ref, "latest": newest, "majors_behind": lag},
        ))
    return signals
