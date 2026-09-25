"""autopilot CLI: scan | file | report | close-resolved | doctor. Every write path is dry-run unless --apply."""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone

from . import doctor, owners, triage
from .github import GitHub, GitHubError, get_token, parse_repo_slug, repo_from_checkout
from .models import PRIORITIES, Issue
from .render import changelog, no_pings, render
from .sources import Context, _registry, run_sources
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


def source_names(args) -> list[str] | None:
    """Parse --sources, failing before any clone or API call on a typo."""
    if not args.sources:
        return None
    names = [n.strip() for n in args.sources.split(",") if n.strip()]
    known = list(_registry())
    if bad := [n for n in names if n not in known]:
        die(f"unknown source(s): {', '.join(bad)} (choose from {', '.join(known)})")
    return names


def check_cap(args) -> None:
    if not 1 <= args.max_issues <= HARD_CAP:
        die(f"--max-issues must be between 1 and {HARD_CAP}")


def collect(args, tmp: str) -> tuple[Context, list[Issue], list[str]]:
    names = source_names(args)
    ctx = build_context(args, tmp)
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
        login, allowed = ctx.gh.write_access(ctx.repo)
    except GitHubError as e:
        die(f"permission check failed: {e}")
    if not allowed:
        die(f"{login} has no push access to {ctx.repo}; refusing to write issues there")
    return login


def existing_bot_issues(ctx: Context) -> dict[str, dict]:
    """Open and closed autopilot issues: closed ones tell us what a human already dismissed."""
    if not (ctx.gh and ctx.repo):
        return {}
    try:
        return triage.index_existing(ctx.gh.issues(ctx.repo, "all"))
    except GitHubError as e:
        die(f"could not list issues on {ctx.repo}: {e}")


def recheck_resolved_closes(ctx: Context, existing: dict[str, dict], issues: list[Issue]) -> None:
    """A labelled close only counts as ours if nobody reopened the issue since; otherwise a human closed it.

    Only live groups matter (they're the ones that could be refiled), so this costs one call per regression."""
    for i in issues:
        gi = existing.get(i.fingerprint)
        if not gi or triage.is_open(gi) or triage.closed_by_human(gi):
            continue
        try:
            events = ctx.gh.paginate(f"/repos/{ctx.repo}/issues/{gi['number']}/events")
        except GitHubError as e:
            ctx.warnings.append(f"could not read events of #{gi['number']} ({e}); treating it as resolved")
            continue
        if triage.reopened_since_resolved(events):
            gi["labels"] = [lb for lb in gi.get("labels", []) if lb["name"] != triage.RESOLVED_LABEL]


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
    check_cap(args)
    with tempfile.TemporaryDirectory() as tmp:
        ctx, issues, _ = collect(args, tmp)
    login = require_write_access(ctx) if args.apply else None
    existing = existing_bot_issues(ctx)
    recheck_resolved_closes(ctx, existing, issues)
    p = triage.plan(issues, existing, args.max_issues, reopen=args.reopen)
    client = None if args.no_llm else make_client()
    for issue in p.create + p.update + p.reopen:
        polish(issue, client) if client else render(issue)

    rules = owners.load_codeowners(ctx.path) if ctx.path else []
    owner = {i.fingerprint: owners.owner_for(ctx.path, i, rules, ctx.gh, ctx.repo)
             for i in p.create + p.update + p.reopen}

    def owner_note(issue: Issue) -> str:
        o = owner.get(issue.fingerprint)
        if not o:
            return ""
        verb = "assign" if args.assign and args.apply else "would assign" if args.assign else "owner"
        return f"\n     {verb}: {o[0]} (via {o[1]})"

    def assignees(issue: Issue, gi: dict | None = None) -> list[str]:
        o = owner.get(issue.fingerprint)
        if not (args.assign and o) or (gi and gi.get("assignees")):  # never override a human's choice
            return []
        return [o[0]]

    mode = "apply" if args.apply else "dry-run"
    where = ctx.repo or "(no GitHub repo)"
    print(f"[{mode}] {len(issues)} issue group(s): {len(p.create)} new, {len(p.update)} changed, "
          f"{len(p.unchanged)} already open, {len(p.suppressed) + len(p.reopen)} closed by a human, "
          f"{len(p.over)} over cap ({args.max_issues}) -> {where}")
    for i in p.unchanged:
        gi = existing[i.fingerprint]
        print(f"  = skip (already open #{gi['number']}): {gi['title']}")
    for i in p.suppressed:
        gi = existing[i.fingerprint]
        print(f"  = skip (closed by a human #{gi['number']}; --reopen to override): {gi['title']}")
    for i in p.over:
        print(f"  ~ deferred (cap reached): {i.kind}: {i.group}")

    edited = 0
    for issue in p.update:
        gi = existing[issue.fingerprint]
        note = changelog(gi.get("body"), issue.body)
        if not args.apply:
            print(f"\n  ~ [would update] #{gi['number']}: {issue.title}{owner_note(issue)}")
            print("    changelog comment:\n" + indent(note) if note else "    (line numbers/format only: edit without comment)")
            continue
        # Keep labels a human added; only swap our own priority label.
        keep = [lb["name"] for lb in gi.get("labels", []) if lb["name"] not in PRIORITIES]
        labels = list(dict.fromkeys(keep + issue.labels))
        fields = {"assignees": a} if (a := assignees(issue, gi)) else {}
        try:
            ctx.gh.update_issue(ctx.repo, gi["number"], title=issue.title, body=issue.body, labels=labels, **fields)
            if note:
                ctx.gh.comment(ctx.repo, gi["number"], note)
        except GitHubError as e:
            print(f"  ! failed to update #{gi['number']}: {e}", file=sys.stderr)
            continue
        edited += 1
        print(f"  ~ updated #{gi['number']}: {issue.title}{owner_note(issue) if fields else ''}")

    reopened = 0
    for issue in p.reopen:
        gi = existing[issue.fingerprint]
        if not args.apply:
            print(f"  ^ [would reopen] #{gi['number']}: {issue.title}{owner_note(issue)}")
            continue
        fields = {"assignees": a} if (a := assignees(issue, gi)) else {}
        try:
            ctx.gh.update_issue(ctx.repo, gi["number"], state="open", title=issue.title, body=issue.body, **fields)
            ctx.gh.comment(ctx.repo, gi["number"], ("issue-autopilot reopened this issue (`--reopen`): its signals "
                                                    "are still present.\n\n" + changelog(gi.get("body"), issue.body)).strip())
        except GitHubError as e:
            print(f"  ! failed to reopen #{gi['number']}: {e}", file=sys.stderr)
            continue
        reopened += 1
        print(f"  ^ reopened #{gi['number']}: {issue.title}")

    created = 0
    for n, issue in enumerate(p.create, 1):
        if not args.apply:
            print(f"\n  {n}. [would create] {issue.title}")
            print(f"     labels: {', '.join(issue.labels)}   fp={issue.fingerprint}{owner_note(issue)}")
            print(indent(issue.body))
            continue
        try:
            gi = ctx.gh.create_issue(ctx.repo, issue.title, issue.body, issue.labels, assignees(issue))
        except GitHubError as e:
            print(f"  ! failed to create '{issue.title}': {e}", file=sys.stderr)
            continue
        created += 1
        print(f"  + created #{gi['number']}: {issue.title}\n    {gi['html_url']}"
              + (owner_note(issue) if assignees(issue) else ""))
    if args.apply:
        print(f"\ncreated {created}, updated {edited}, reopened {reopened} issue(s) on {ctx.repo} as {login}")
    else:
        print("\n(dry run: nothing was written; re-run with --apply to create/update these issues)")
    return 0


def cmd_close_resolved(args) -> int:
    check_cap(args)
    with tempfile.TemporaryDirectory() as tmp:
        ctx, issues, ran = collect(args, tmp)
    if not (ctx.gh and ctx.repo):
        die("close-resolved needs a GitHub repo and a token")
    if args.apply:
        require_write_access(ctx)
    existing = existing_bot_issues(ctx)
    gone = triage.resolved(existing, issues, ran)[: args.max_issues]
    mode = "apply" if args.apply else "dry-run"
    n_open = sum(map(triage.is_open, existing.values()))
    print(f"[{mode}] {n_open} open autopilot issue(s) on {ctx.repo}; {len(gone)} resolved "
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
            # The label is how `file` tells our closes from a human's; add it before closing.
            ctx.gh.add_labels(ctx.repo, gi["number"], [triage.RESOLVED_LABEL])
            ctx.gh.close_issue(ctx.repo, gi["number"])
            print(f"  x closed #{gi['number']}: {gi['title']}")
        except GitHubError as e:
            print(f"  ! failed to close #{gi['number']}: {e}", file=sys.stderr)
    return 0


def report_md(ctx: Context, issues: list[Issue], ran: list[str], existing: dict[str, dict]) -> str:
    p = triage.plan(issues, existing, cap=max(len(issues), 1))
    status = {id(i): s for s, group in (("new", p.create), ("changed", p.update), ("up to date", p.unchanged),
                                        ("closed by a human", p.suppressed)) for i in group}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# issue-autopilot report: {ctx.repo or ctx.path}", "",
           f"_Generated {now}. Sources: {', '.join(ran) or 'none'}._", "",
           "| Priority | Groups | Signals |", "|---|---:|---:|"]
    for pri in PRIORITIES:
        grp = [i for i in issues if i.priority == pri]
        out.append(f"| {pri} | {len(grp)} | {sum(len(i.signals) for i in grp)} |")
    out += ["", "## Groups", ""]
    if issues:
        out += ["| Priority | Kind | Group | Signals | Issue | Status |", "|---|---|---|---:|---|---|"]
        for i in issues:
            gi = existing.get(i.fingerprint)
            ref = f"#{gi['number']}" if gi and status[id(i)] != "new" else "-"
            group = i.group.replace("|", "\\|")
            out.append(f"| {i.priority} | {i.kind} | `{group}` | {len(i.signals)} | {ref} | {status[id(i)]} |")
    else:
        out.append("No signals found.")
    gone = triage.resolved(existing, issues, ran)
    if gone:
        out += ["", "## Resolved (open issues whose signals are gone)", ""]
        out += [f"- #{gi['number']} {gi['title']}" for gi in gone]
    if ctx.warnings:
        out += ["", "## Warnings", ""] + [f"- {w}" for w in ctx.warnings]
    return no_pings("\n".join(out)) + "\n"


def cmd_report(args) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ctx, issues, ran = collect(args, tmp)
    existing = existing_bot_issues(ctx)
    recheck_resolved_closes(ctx, existing, issues)
    md = report_md(ctx, issues, ran, existing)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(md)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(md, end="")
    return 0


def cmd_doctor(args) -> int:
    checks = doctor.run_checks(args.target, args.repo, source_names(args))
    if args.json:
        print(json.dumps([{"status": st, "check": name, "detail": d} for st, name, d in checks], indent=2))
    else:
        for st, name, d in checks:
            print(f"[{st:>4}] {name}: {d}")
    return 1 if any(st == doctor.FAIL for st, _, _ in checks) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autopilot", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, target_default=None):
        if target_default is None:
            sp.add_argument("target", help="local path or owner/repo")
        else:
            sp.add_argument("target", nargs="?", default=target_default, help="local path or owner/repo (default: .)")
        sp.add_argument("--repo", help="owner/repo for API sources and filing (default: origin remote)")
        sp.add_argument("--sources", help="comma-separated: todo,secret,deps,advisory,ci,actions,stale-pr (default: all)")
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
    f.add_argument("--assign", action="store_true",
                   help="assign each issue to its CODEOWNERS / git blame owner (only written with --apply)")
    f.add_argument("--reopen", action="store_true",
                   help="reopen autopilot issues a human closed if their signals are still present")
    f.set_defaults(func=cmd_file)

    r = sub.add_parser("report", help="markdown summary of signals and their issues (read-only)")
    common(r, ".")
    r.add_argument("--out", metavar="FILE", help="write the report here instead of stdout")
    r.set_defaults(func=cmd_report)

    c = sub.add_parser("close-resolved", help="close autopilot issues whose signals are gone (dry-run unless --apply)")
    common(c, ".")
    c.add_argument("--apply", action="store_true", help="actually close issues")
    c.add_argument("--max-issues", type=int, default=DEFAULT_CAP, help="max issues to close per run")
    c.set_defaults(func=cmd_close_resolved)
    d = sub.add_parser("doctor", help="check token, repo access and source prerequisites (read-only)")
    d.add_argument("target", nargs="?", default=".", help="local path or owner/repo (default: .)")
    d.add_argument("--repo", help="owner/repo (default: origin remote)")
    d.add_argument("--sources", help="only check what these sources need")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
