import pytest

from issue_autopilot import cli, render, triage
from issue_autopilot.sources import Context, todos

from helpers import make_repo

API = "https://api.github.com"
ISSUES = f"{API}/repos/o/r/issues?state=open&per_page=100"
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

    http.add("POST", f"{API}/repos/o/r/issues/2/comments", {})
    http.add("PATCH", f"{API}/repos/o/r/issues/2", {})
    run("close-resolved", repo, "--repo", "o/r", "--sources", "todo", "--apply")
    assert [(m, u.rsplit("/", 2)[-2:]) for m, u, _ in writes(http)] == [
        ("POST", ["2", "comments"]), ("PATCH", ["issues", "2"])]


def test_scan_json(repo, http, capsys):
    run("scan", repo, "--sources", "todo", "--json")
    import json
    data = json.loads(capsys.readouterr().out)
    assert sorted(d["group"] for d in data) == ["a.py", "b.py", "c.py"]
