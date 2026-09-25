"""autopilot CLI: scan | file | close-resolved. Every write path is dry-run unless --apply."""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict

from . import triage
from .github import GitHub, GitHubError, get_token, parse_repo_slug, repo_from_checkout
from .models import Issue
from .render import render
from .sources import Context, run_sources
from .summarize import make_client, polish

DEFAULT_CAP = 10
HARD_CAP = 50


def die(msg: str, code: int = 2):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def clone(repo: str, token: str | None, dest: str) -> None:
    env = dict(os.environ)
    if token:  # pass auth via env-scoped git config so it never lands in argv or .git/config
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    r = subprocess.run(["git", "clone", "--quiet", "--no-tags", f"https://github.com/{repo}.git", dest],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"could not clone {repo}: {r.stderr.strip()}")


def build_context(args, tmp: str) -> Context:
    target = args.target
    token = get_token()
    gh = GitHub(token) if token else None
    if os.path.isdir(target):
        path, repo = target, args.repo or repo_from_checkout(target)
    else:
        repo = parse_repo_slug(target)
        if not repo:
            die(f"{target!r} is neither a directory nor owner/repo")
        path = os.path.join(tmp, "checkout")
        print(f"cloning {repo} ...", file=sys.stderr)
        clone(repo, token, path)
        if args.repo and args.repo != repo:
            die("--repo conflicts with the owner/repo target")
    return Context(path=path, repo=repo, gh=gh, options={"stale_days": args.stale_days, "exclude": args.exclude})


def collect(args, tmp: str) -> tuple[Context, list[Issue], list[str]]:
    ctx = build_context(args, tmp)
    names = args.sources.split(",") if args.sources else None
    signals, ran = run_sources(ctx, names)
    for w in ctx.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return ctx, triage.group(signals), ran


def require_write_access(ctx: Context) -> str:
    """Refuse to write unless we're authenticated and have push access to the target repo."""
    if not ctx.gh:
        die("--apply needs a token (GITHUB_TOKEN or `gh auth login`)")
    if not ctx.repo:
        die("--apply needs a GitHub repo (pass owner/repo or --repo, or run inside a checkout with an origin)")
    try:
        login = ctx.gh.whoami()
        allowed = ctx.gh.can_push(ctx.repo)
    except GitHubError as e:
        die(f"permission check failed: {e}")
    if not allowed:
        die(f"{login} has no push access to {ctx.repo}; refusing to write issues there")
    return login


def existing_bot_issues(ctx: Context) -> dict[str, dict]:
    if not (ctx.gh and ctx.repo):
        return {}
    try:
        return triage.index_existing(ctx.gh.open_issues(ctx.repo))
    except GitHubError as e:
        die(f"could not list open issues on {ctx.repo}: {e}")


def indent(text: str, pad: str = "     ") -> str:
    return "\n".join(pad + ln for ln in text.splitlines())


# --- commands -----------------------------------------------------------------
def cmd_scan(args) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ctx, issues, ran = collect(args, tmp)
    if args.json:
        print(json.dumps([{"kind": i.kind, "group": i.group, "priority": i.priority, "fingerprint": i.fingerprint,
                           "signals": [asdict(s) for s in i.signals]} for i in issues], indent=2))
        return 0
    total = sum(len(i.signals) for i in issues)
    print(f"scanned {ctx.repo or ctx.path} with sources: {', '.join(ran) or 'none'}")
    print(f"{total} signal(s) in {len(issues)} group(s)\n")
    for i in issues:
        print(f"[{i.priority}] {i.kind}: {i.group}  (fp={i.fingerprint})")
        for s in i.signals[:20]:
            loc = f"{s.path}:{s.line} " if s.line else ""
            print(f"     - {loc}{s.summary}")
        if len(i.signals) > 20:
            print(f"     - ...and {len(i.signals) - 20} more")
    return 0


def cmd_file(args) -> int:
    if not 1 <= args.max_issues <= HARD_CAP:
        die(f"--max-issues must be between 1 and {HARD_CAP}")
    with tempfile.TemporaryDirectory() as tmp:
        ctx, issues, _ = collect(args, tmp)
    login = require_write_access(ctx) if args.apply else None
    existing = existing_bot_issues(ctx)
    new, dupes, over = triage.plan(issues, existing, args.max_issues)
    client = None if args.no_llm else make_client()
    for issue in new:
        polish(issue, client) if client else render(issue)

    mode = "apply" if args.apply else "dry-run"
    where = ctx.repo or "(no GitHub repo)"
    print(f"[{mode}] {len(issues)} issue group(s): {len(new)} new, {len(dupes)} already open, "
          f"{len(over)} over cap ({args.max_issues}) -> {where}")
    for i in dupes:
        gi = existing[i.fingerprint]
        print(f"  = skip (already open #{gi['number']}): {gi['title']}")
    for i in over:
        print(f"  ~ deferred (cap reached): {i.kind}: {i.group}")
    created = 0
    for n, issue in enumerate(new, 1):
        if not args.apply:
            print(f"\n  {n}. [would create] {issue.title}")
            print(f"     labels: {', '.join(issue.labels)}   fp={issue.fingerprint}")
            print(indent(issue.body))
            continue
        try:
            gi = ctx.gh.create_issue(ctx.repo, issue.title, issue.body, issue.labels)
        except GitHubError as e:
            print(f"  ! failed to create '{issue.title}': {e}", file=sys.stderr)
            continue
        created += 1
        print(f"  + created #{gi['number']}: {issue.title}\n    {gi['html_url']}")
    if args.apply:
        print(f"\ncreated {created} issue(s) on {ctx.repo} as {login}")
    else:
        print("\n(dry run: nothing was written; re-run with --apply to create these issues)")
    return 0


def cmd_close_resolved(args) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ctx, issues, ran = collect(args, tmp)
    if not (ctx.gh and ctx.repo):
        die("close-resolved needs a GitHub repo and a token")
    if args.apply:
        require_write_access(ctx)
    existing = existing_bot_issues(ctx)
    gone = triage.resolved(existing, issues, ran)[: args.max_issues]
    mode = "apply" if args.apply else "dry-run"
    print(f"[{mode}] {len(existing)} open autopilot issue(s) on {ctx.repo}; {len(gone)} resolved "
          f"(checked sources: {', '.join(ran)})")
    for gi in gone:
        if not args.apply:
            print(f"  - would close #{gi['number']}: {gi['title']}")
            continue
        try:
            # The issue list can lag a few seconds behind; re-check so re-runs never double-comment.
            if ctx.gh.get(f"/repos/{ctx.repo}/issues/{gi['number']}")["state"] != "open":
                print(f"  = already closed #{gi['number']}")
                continue
            ctx.gh.comment(ctx.repo, gi["number"], "issue-autopilot: the signals behind this issue are no longer "
                                                  "detected, closing as resolved.")
            ctx.gh.close_issue(ctx.repo, gi["number"])
            print(f"  x closed #{gi['number']}: {gi['title']}")
        except GitHubError as e:
            print(f"  ! failed to close #{gi['number']}: {e}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, target_default=None):
        if target_default is None:
            sp.add_argument("target", help="local path or owner/repo")
        else:
            sp.add_argument("target", nargs="?", default=target_default, help="local path or owner/repo (default: .)")
        sp.add_argument("--repo", help="owner/repo for API sources and filing (default: origin remote)")
        sp.add_argument("--sources", help="comma-separated: todo,secret,deps,ci,stale-pr (default: all)")
        sp.add_argument("--stale-days", type=int, default=30, help="PR idle days before it counts as stale")
        sp.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip files matching this glob for file-based sources (repeatable), e.g. 'tests/*'")

    s = sub.add_parser("scan", help="list signals (read-only)")
    common(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_scan)

    f = sub.add_parser("file", help="file issues for new signals (dry-run unless --apply)")
    common(f, ".")
    f.add_argument("--apply", action="store_true", help="actually create issues")
    f.add_argument("--max-issues", type=int, default=DEFAULT_CAP, help=f"per-run cap (default {DEFAULT_CAP})")
    f.add_argument("--no-llm", action="store_true", help="skip the Claude summarizer even if a key is set")
    f.set_defaults(func=cmd_file)

    c = sub.add_parser("close-resolved", help="close autopilot issues whose signals are gone (dry-run unless --apply)")
    common(c, ".")
    c.add_argument("--apply", action="store_true", help="actually close issues")
    c.add_argument("--max-issues", type=int, default=DEFAULT_CAP, help="max issues to close per run")
    c.set_defaults(func=cmd_close_resolved)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
