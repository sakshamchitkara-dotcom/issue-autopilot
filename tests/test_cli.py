import pytest

from issue_autopilot import cli, render, triage
from issue_autopilot.sources import Context, todos

from helpers import make_repo

API = "https://api.github.com"
ISSUES = f"{API}/repos/o/r/issues?state=all&per_page=100"
FILES = {"a.py": "# TODO: one\n", "b.py": "# FIXME: two\n", "c.py": "# HACK: three\n"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return make_repo(tmp_path, FILES)


def run(*argv):
    return cli.main(list(argv))


def base_routes(http, push=True, open_issues=()):
    http.add("GET", f"{API}/user", {"login": "me"})
    http.add("GET", f"{API}/repos/o/r", {"permissions": {"push": push}, "default_branch": "main"})
    http.add("GET", ISSUES, list(open_issues))


def writes(http):
    return [c for c in http.calls if c[0] in ("POST", "PATCH", "PUT", "DELETE")]


def test_file_defaults_to_dry_run(repo, http, capsys):
    base_routes(http)
    run("file", repo, "--repo", "o/r", "--sources", "todo")
    out = capsys.readouterr().out
    assert "[dry-run] 3 issue group(s): 3 new" in out and "would create" in out
    assert "<!-- issue-autopilot fp=" in out
    assert writes(http) == []


def test_apply_refuses_without_push_access(repo, http, capsys):
    base_routes(http, push=False)
    with pytest.raises(SystemExit) as e:
        run("file", repo, "--repo", "o/r", "--sources", "todo", "--apply")
    assert e.value.code == 2 and writes(http) == []
    assert "no push access" in capsys.readouterr().err


def test_apply_creates_new_respects_cap_and_dedupes(repo, http, capsys):
    # pre-existing open issue for b.py -> must be skipped
    sigs = todos.scan(Context(path=repo, repo=None, gh=None))
    existing = render.render([i for i in triage.group(sigs) if i.group == "b.py"][0])
    base_routes(http, open_issues=[{"number": 4, "title": existing.title, "body": existing.body}])
    http.add("POST", f"{API}/repos/o/r/issues", {"number": 5, "html_url": "https://github.com/o/r/issues/5"})
    run("file", repo, "--repo", "o/r", "--sources", "todo", "--apply", "--max-issues", "1")
    out = capsys.readouterr().out
    posted = writes(http)
    assert len(posted) == 1
    assert "b.py" not in posted[0][2]["title"] and posted[0][2]["labels"][0] == "autopilot"
    assert "skip (already open #4)" in out and "deferred (cap reached)" in out


def test_max_issues_hard_cap(repo):
    with pytest.raises(SystemExit):
        run("file", repo, "--repo", "o/r", "--max-issues", "500")


def test_close_resolved(repo, http, capsys):
    stale = render.render(triage.group([todos.Signal("todo", "gone.py", "TODO: x", path="gone.py", line=1)])[0])
    live = render.render(triage.group(todos.scan(Context(path=repo, repo=None, gh=None)))[0])
    other_kind = render.render(triage.group([todos.Signal("deps", "requirements.txt", "x 1 → 2")])[0])
    human = {"number": 1, "title": "human issue", "body": "no marker"}
    base_routes(http, open_issues=[
        human, {"number": 2, "title": stale.title, "body": stale.body},
        {"number": 3, "title": live.title, "body": live.body},
        {"number": 9, "title": other_kind.title, "body": other_kind.body}])
    run("close-resolved", repo, "--repo", "o/r", "--sources", "todo")
    assert "would close #2" in capsys.readouterr().out and writes(http) == []

    http.add("GET", f"{API}/repos/o/r/issues/2", {"state": "open"})
    http.add("POST", f"{API}/repos/o/r/issues/2/comments", {})
    http.add("POST", f"{API}/repos/o/r/issues/2/labels", [])
    http.add("PATCH", f"{API}/repos/o/r/issues/2", {})
    run("close-resolved", repo, "--repo", "o/r", "--sources", "todo", "--apply")
    assert [(m, u.rsplit("/", 2)[-2:]) for m, u, _ in writes(http)] == [
        ("POST", ["2", "comments"]), ("POST", ["2", "labels"]), ("PATCH", ["issues", "2"])]
    assert writes(http)[1][2] == {"labels": ["autopilot:resolved"]}


def test_scan_json(repo, http, capsys):
    run("scan", repo, "--sources", "todo", "--json")
    import json
    data = json.loads(capsys.readouterr().out)
    assert sorted(d["group"] for d in data) == ["a.py", "b.py", "c.py"]


def test_close_resolved_skips_already_closed(repo, http, capsys):
    stale = render.render(triage.group([todos.Signal("todo", "gone.py", "TODO: x", path="gone.py", line=1)])[0])
    base_routes(http, open_issues=[{"number": 2, "title": stale.title, "body": stale.body}])
    http.add("GET", f"{API}/repos/o/r/issues/2", {"state": "closed"})  # list lagged behind
    run("close-resolved", repo, "--repo", "o/r", "--sources", "todo", "--apply")
    assert writes(http) == [] and "already closed #2" in capsys.readouterr().out


def test_changed_group_is_edited_in_place_with_changelog(repo, http, capsys):
    # issue for a.py was filed when it only had a different TODO
    old = render.render(triage.group([todos.Signal("todo", "a.py", "TODO: old", path="a.py", line=1)])[0])
    labels = [{"name": "autopilot"}, {"name": "tech-debt"}, {"name": "P3"}, {"name": "needs-triage"}]
    base_routes(http, open_issues=[{"number": 7, "title": old.title, "body": old.body, "labels": labels}])
    run("file", repo, "--repo", "o/r", "--sources", "todo")
    out = capsys.readouterr().out
    assert "1 changed" in out and "[would update] #7" in out and writes(http) == []

    http.add("PATCH", f"{API}/repos/o/r/issues/7", {})
    http.add("POST", f"{API}/repos/o/r/issues/7/comments", {})
    http.add("POST", f"{API}/repos/o/r/issues", {"number": 8, "html_url": "u"})
    run("file", repo, "--repo", "o/r", "--sources", "todo", "--apply")
    edit = [c for c in writes(http) if c[1].endswith("/issues/7")][0][2]
    assert "needs-triage" in edit["labels"] and "TODO: one" in edit["body"]
    note = [c for c in writes(http) if c[1].endswith("/issues/7/comments")][0][2]["body"]
    assert "**Added**" in note and "TODO: one" in note and "**Removed**" in note and "TODO: old" in note
    assert "updated #7" in capsys.readouterr().out


def closed_issue_for(repo, group, number, labels=()):
    sigs = todos.scan(Context(path=repo, repo=None, gh=None))
    issue = render.render([i for i in triage.group(sigs) if i.group == group][0])
    return {"number": number, "title": issue.title, "body": issue.body, "state": "closed",
            "labels": [{"name": n} for n in ("autopilot", *labels)]}


def test_human_close_is_respected_and_reopen_overrides(repo, http, capsys):
    base_routes(http, open_issues=[closed_issue_for(repo, "a.py", 3)])
    http.add("POST", f"{API}/repos/o/r/issues", {"number": 9, "html_url": "u"})
    run("file", repo, "--repo", "o/r", "--sources", "todo", "--apply")
    out = capsys.readouterr().out
    assert "closed by a human #3; --reopen to override" in out
    assert all("a.py" not in c[2]["title"] for c in writes(http)) and len(writes(http)) == 2

    http.calls.clear()
    http.add("PATCH", f"{API}/repos/o/r/issues/3", {})
    http.add("POST", f"{API}/repos/o/r/issues/3/comments", {})
    run("file", repo, "--repo", "o/r", "--sources", "todo", "--apply", "--reopen", "--max-issues", "1")
    assert [(m, u.split("/o/r/")[1]) for m, u, _ in writes(http)] == [
        ("PATCH", "issues/3"), ("POST", "issues/3/comments")]
    assert writes(http)[0][2]["state"] == "open" and "reopened #3" in capsys.readouterr().out


def test_signal_back_after_autopilot_close_is_refiled(repo, http, capsys):
    base_routes(http, open_issues=[closed_issue_for(repo, "a.py", 3, labels=["autopilot:resolved"])])
    http.add("GET", f"{API}/repos/o/r/issues/3/events?per_page=100", [
        {"event": "labeled", "label": {"name": "autopilot:resolved"}}, {"event": "closed"}])
    run("file", repo, "--repo", "o/r", "--sources", "todo")
    out = capsys.readouterr().out
    assert "3 new" in out and "closed by a human #3" not in out


def test_assign_is_dry_run_unless_flagged_and_applied(repo, http, capsys):
    with open(f"{repo}/CODEOWNERS", "w") as fh:
        fh.write("a.py @alice\n")
    base_routes(http)
    http.add("POST", f"{API}/repos/o/r/issues", {"number": 5, "html_url": "u"})
    http.add("GET", f"{API}/repos/o/r/commits/", {"author": None})
    run("file", repo, "--repo", "o/r", "--sources", "todo", "--assign")
    assert "would assign: alice (via CODEOWNERS)" in capsys.readouterr().out and writes(http) == []

    run("file", repo, "--repo", "o/r", "--sources", "todo", "--apply")
    assert all("assignees" not in c[2] for c in writes(http))

    http.calls.clear()
    run("file", repo, "--repo", "o/r", "--sources", "todo", "--apply", "--assign")
    assigned = {c[2]["title"]: c[2].get("assignees") for c in writes(http)}
    assert assigned["Tech debt: 1 TODO comment in a.py"] == ["alice"]
    assert assigned["Tech debt: 1 FIXME comment in b.py"] is None  # Ada's email maps to no account


def test_report_markdown_statuses(repo, http, capsys, tmp_path):
    gone = render.render(triage.group([todos.Signal("todo", "gone.py", "TODO: x", path="gone.py", line=1)])[0])
    base_routes(http, open_issues=[closed_issue_for(repo, "a.py", 3),
                                   {"number": 4, "title": gone.title, "body": gone.body, "state": "open"}])
    out_file = tmp_path.parent / "report.md"
    run("report", repo, "--repo", "o/r", "--sources", "todo", "--out", str(out_file))
    md = out_file.read_text()
    assert md.startswith("# issue-autopilot report: o/r") and "| P2 | 2 | 2 |" in md
    assert "| P2 | todo | `b.py` | 1 | - | new |" in md
    assert "| P3 | todo | `a.py` | 1 | #3 | closed by a human |" in md
    assert "- #4 Tech debt: 1 TODO comment in gone.py" in md and writes(http) == []


def test_line_shift_only_edits_silently(repo, http, capsys):
    cur = closed_issue_for(repo, "a.py", 7)
    legacy = {**cur, "state": "open", "body": cur["body"].replace("a.py:1", "a.py:40").split(" sig=")[0] + " -->"}
    base_routes(http, open_issues=[legacy])
    http.add("PATCH", f"{API}/repos/o/r/issues/7", {})
    http.add("POST", f"{API}/repos/o/r/issues", {"number": 8, "html_url": "u"})
    run("file", repo, "--repo", "o/r", "--sources", "todo", "--apply")
    assert [(m, u) for m, u, _ in writes(http) if "/issues/7" in u] == [("PATCH", f"{API}/repos/o/r/issues/7")]


def test_human_reopen_then_close_of_resolved_issue_is_respected(repo, http, capsys):
    base_routes(http, open_issues=[closed_issue_for(repo, "a.py", 3, labels=["autopilot:resolved"])])
    http.add("GET", f"{API}/repos/o/r/issues/3/events?per_page=100", [
        {"event": "labeled", "label": {"name": "autopilot:resolved"}}, {"event": "closed"},
        {"event": "reopened"}, {"event": "closed"}])
    run("file", repo, "--repo", "o/r", "--sources", "todo")
    out = capsys.readouterr().out
    assert "2 new" in out and "closed by a human #3; --reopen to override" in out


def test_bad_sources_and_caps_fail_before_any_work(repo, http, capsys):
    with pytest.raises(SystemExit):
        run("scan", "octo/cat", "--sources", "todo, nope")  # would clone if not rejected first
    assert "unknown source(s): nope" in capsys.readouterr().err and http.calls == []
    with pytest.raises(SystemExit):
        run("close-resolved", repo, "--repo", "o/r", "--max-issues", "0")
    assert "--max-issues must be between 1 and 50" in capsys.readouterr().err


def test_sources_list_tolerates_spaces(repo, http, capsys):
    run("scan", repo, "--sources", "todo, secret")
    assert "sources: todo, secret" in capsys.readouterr().out
