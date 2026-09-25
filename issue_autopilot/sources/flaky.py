"""Flaky CI jobs: jobs that failed and then passed on the same commit, found in recent Actions runs."""
from __future__ import annotations

from collections import defaultdict

from ..models import Signal
from . import Context

RUNS = 500  # recent completed runs to look at (5 API pages)
MAX_LOOKUPS = 40  # job listings fetched per scan
MIN_FLIPS = 2  # one lucky rerun is noise; twice is a pattern


def _failed_jobs(gh, path: str) -> list[str]:
    return [j["name"] for j in (gh.get(path) or {}).get("jobs", []) if j.get("conclusion") == "failure"]


def scan(ctx: Context) -> list[Signal] | None:
    if not (ctx.repo and ctx.gh):
        return None
    gh, repo = ctx.gh, ctx.repo
    runs = gh.paginate(f"/repos/{repo}/actions/runs?status=completed", limit=RUNS)

    # (a) a run that passed on attempt N > 1: the jobs that failed in attempt N-1 are flaky
    lookups = [(r, f"/repos/{repo}/actions/runs/{r['id']}/attempts/{r['run_attempt'] - 1}/jobs")
               for r in runs if r.get("run_attempt", 1) > 1 and r.get("conclusion") == "success"]
    # (b) separate runs of one workflow on one commit that disagree: the failed run's jobs are flaky
    by_commit = defaultdict(list)
    for r in runs:
        by_commit[(r["workflow_id"], r.get("head_sha"))].append(r)
    for same in by_commit.values():
        if any(r.get("conclusion") == "success" for r in same):
            lookups += [(r, f"/repos/{repo}/actions/runs/{r['id']}/jobs?filter=latest")
                        for r in same if r.get("conclusion") == "failure"]

    flips: dict[tuple[str, str], list[str]] = defaultdict(list)  # (workflow path, job) -> short shas
    names = {}
    for run, path in lookups[:MAX_LOOKUPS]:
        wf = run.get("path") or run.get("name", "workflow")
        names[wf] = run.get("name") or wf
        for job in _failed_jobs(gh, path):
            flips[(wf, job)].append((run.get("head_sha") or "")[:7])
    if len(lookups) > MAX_LOOKUPS:
        ctx.warnings.append(f"flaky: checked {MAX_LOOKUPS} of {len(lookups)} retried runs")

    signals = []
    for (wf, job), shas in sorted(flips.items()):
        if len(shas) < MIN_FLIPS:
            continue
        signals.append(Signal(
            kind="flaky",
            group=wf,
            # counts stay out of the summary so the issue isn't edited every time the window moves
            summary=f"job `{job}` in '{names[wf]}' failed, then passed on the same commit",
            priority="P2",
            path=wf if wf.startswith(".github/") else None,
            detail=f"`{job}`: {len(shas)} fail-then-pass flips in the last {len(runs)} completed runs "
                   f"(commits {', '.join(dict.fromkeys(shas))})",
            meta={"job": job, "flips": len(shas)},
        ))
    return signals
