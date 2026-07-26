"""`autopilot doctor`: check token, repo access and per-source prerequisites without writing anything."""
from __future__ import annotations

import os
import shutil
import subprocess

from . import owners
from .github import GitHub, GitHubError, get_token, parse_repo_slug, repo_from_checkout

OK, WARN, FAIL = "ok", "warn", "fail"


def token_source() -> str | None:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return var
    return "gh auth token" if get_token() else None


def run_checks(target: str, repo: str | None, sources: list[str] | None) -> list[tuple[str, str, str]]:
    """[(status, check, detail)]. Read-only: every call is a GET."""
    out: list[tuple[str, str, str]] = []
    add = lambda status, name, detail: out.append((status, name, detail))  # noqa: E731
    wanted = lambda name: sources is None or name in sources  # noqa: E731

    add(OK if shutil.which("git") else FAIL, "git", shutil.which("git") or "git is not on PATH")
    local = os.path.isdir(target)
    if local:
        repo = repo or repo_from_checkout(target)
        inside = subprocess.run(["git", "-C", target, "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True).returncode == 0
        add(OK if inside else WARN, "checkout",
            target if inside else f"{target} is not a git checkout: every file is scanned and blame is unavailable")
    else:
        repo = parse_repo_slug(target)
        add(OK if repo else FAIL, "target", f"remote {repo}" if repo else f"{target!r} is neither a directory nor owner/repo")
    add(OK if repo else WARN, "repo", repo or "no GitHub repo: API sources (advisory alerts, ci, stale-pr) and --apply are unavailable")

    src = token_source()
    token = get_token()
    add(OK if token else WARN, "token", f"from {src}" if token else "none: set GITHUB_TOKEN or run `gh auth login`")

    if token and repo:
        gh = GitHub(token)
        try:
            who, push = gh.write_access(repo)
            add(OK, "identity", who)
            add(OK if push else WARN, "push access", "yes: --apply can write issues" if push
                else "no: dry runs work, --apply will be refused")
        except GitHubError as e:
            add(FAIL, "identity", str(e))
        for name, path, source, missing in (
            ("issues", f"/repos/{repo}/issues?state=all&per_page=1", None, FAIL),
            ("dependabot alerts", f"/repos/{repo}/dependabot/alerts?state=open&per_page=1", "advisory", WARN),
            ("actions runs", f"/repos/{repo}/actions/runs?per_page=1", "ci", WARN),
            ("pull requests", f"/repos/{repo}/pulls?state=open&per_page=1", "stale-pr", WARN),
        ):
            if source and not wanted(source):
                continue
            try:
                gh.get(path)
                add(OK, name, "readable")
            except GitHubError as e:
                hint = {"advisory": "; advisory falls back to OSV.dev"}.get(source or "", "")
                add(missing, name, f"not readable ({e.status}){hint}")

    if local:
        rules = owners.load_codeowners(target)
        teams = sorted({o for _, os_ in rules for o in os_ if "/" in o})
        detail = f"{len(rules)} rule(s)" + (f"; team owners can't be assigned: {', '.join(teams)}" if teams else "")
        add(OK if rules else WARN, "CODEOWNERS", detail if rules else "none: --assign falls back to git blame")

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            add(OK, "summarizer", "ANTHROPIC_API_KEY set and anthropic installed")
        except ImportError:
            add(WARN, "summarizer", "ANTHROPIC_API_KEY set but `anthropic` is not installed (pip install .[llm])")
    else:
        add(OK, "summarizer", "off (no ANTHROPIC_API_KEY): templates are used")
    return out
