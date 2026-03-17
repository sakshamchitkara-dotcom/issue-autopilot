"""Failing GitHub Actions workflows (latest run on the default branch), with a log excerpt."""
from __future__ import annotations

import re
import urllib.parse

from ..models import Signal
from . import Context

TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR = re.compile(r"##\[error\]|\berror\b|\bERROR\b|\bFAILED\b|Traceback|\bfailed\b|AssertionError|Exception", re.I)
NOISE = re.compile(r"Process completed with exit code|^##\[(?:end)?group\]|^\[command\]|^Post job cleanup")


def extract_error(log: str, before: int = 12, after: int = 2, cap: int = 40) -> str:
    lines = [ANSI.sub("", TIMESTAMP.sub("", ln)).rstrip() for ln in log.splitlines()]
    lines = [ln for ln in lines if not NOISE.search(ln)]
    hits = [i for i, ln in enumerate(lines) if ERROR.search(ln)]
    if not hits:
        return "\n".join(lines[-cap:])
    i = hits[-1]
    return "\n".join(lines[max(0, i - before): i + after + 1][-cap:])


def scan(ctx: Context) -> list[Signal] | None:
    if not (ctx.repo and ctx.gh):
        return None
    gh, repo = ctx.gh, ctx.repo
    branch = gh.repo(repo)["default_branch"]
    runs = gh.paginate(
        f"/repos/{repo}/actions/runs?branch={urllib.parse.quote(branch)}&status=completed", limit=100
    )
    latest: dict = {}
    for run in runs:  # don't trust API ordering across pages; pick newest explicitly
        cur = latest.get(run["workflow_id"])
        if cur is None or run["created_at"] > cur["created_at"]:
            latest[run["workflow_id"]] = run
    signals = []
    for run in latest.values():
        if run.get("conclusion") != "failure":
            continue
        jobs = gh.get(f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest").get("jobs", [])
        failed = [j for j in jobs if j.get("conclusion") == "failure"]
        excerpts = []
        for job in failed[:3]:
            try:
                excerpt = extract_error(gh.text(f"/repos/{repo}/actions/jobs/{job['id']}/logs"))
            except Exception as e:  # logs expire / need extra perms; still report the failure
                excerpt = f"(log unavailable: {e})"
            excerpts.append(f"Job `{job['name']}`:\n{excerpt}")
        workflow = run.get("name") or run.get("path", "workflow")
        signals.append(
            Signal(
                kind="ci",
                group=run.get("path") or workflow,
                summary=f"Workflow '{workflow}' is failing on {branch}",
                priority="P1",
                path=run.get("path"),
                detail="\n\n".join(excerpts),
                meta={
                    "run_url": run.get("html_url"),
                    "sha": (run.get("head_sha") or "")[:7],
                    "failed_jobs": [j["name"] for j in failed],
                },
            )
        )
    return signals
