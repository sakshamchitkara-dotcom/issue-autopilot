from datetime import datetime, timezone

from issue_autopilot.github import GitHub
from issue_autopilot.sources import Context, actions, prs

API = "https://api.github.com"

LOG = """2024-05-01T10:00:00.0000000Z ##[group]Run pytest
2024-05-01T10:00:01.0000000Z ##[endgroup]
2024-05-01T10:00:02.0000000Z tests/test_x.py::test_add FAILED
2024-05-01T10:00:03.0000000Z E   AssertionError: assert 3 == 4
2024-05-01T10:00:04.0000000Z ##[error]Process completed with exit code 1.
"""


def ctx():
    return Context(path=None, repo="o/r", gh=GitHub("t"))


def test_extract_error_strips_noise():
    out = actions.extract_error(LOG)
    assert "AssertionError: assert 3 == 4" in out and "FAILED" in out
    assert "exit code" not in out and "2024-05-01T" not in out


def test_failing_workflow_signal(http):
    http.add("GET", f"{API}/repos/o/r", {"default_branch": "main"})
    http.add("GET", f"{API}/repos/o/r/actions/runs?branch=main&status=completed&per_page=100", {"workflow_runs": [
        {"id": 2, "workflow_id": 1, "conclusion": "success", "name": "CI", "created_at": "2024-05-01T09:00:00Z"},
        {"id": 3, "workflow_id": 1, "conclusion": "failure", "name": "CI", "path": ".github/workflows/ci.yml",
         "html_url": "https://gh/run/3", "head_sha": "abcdef1234", "created_at": "2024-05-01T10:00:00Z"},
        {"id": 1, "workflow_id": 7, "conclusion": "success", "name": "Lint", "created_at": "2024-05-01T08:00:00Z"},
        {"id": 0, "workflow_id": 7, "conclusion": "failure", "name": "Lint", "created_at": "2024-04-01T08:00:00Z"},
    ]})
    http.add("GET", f"{API}/repos/o/r/actions/runs/3/jobs?filter=latest",
             {"jobs": [{"id": 30, "name": "test", "conclusion": "failure"}, {"id": 31, "name": "ok", "conclusion": "success"}]})
    http.add("GET", f"{API}/repos/o/r/actions/jobs/30/logs", LOG.encode())
    [sig] = actions.scan(ctx())
    assert sig.group == ".github/workflows/ci.yml" and sig.priority == "P1"
    assert "AssertionError" in sig.detail and sig.meta["failed_jobs"] == ["test"]


def test_stale_prs(http):
    http.add("GET", f"{API}/repos/o/r/pulls?state=open&sort=updated&direction=asc&per_page=100", [
        {"number": 5, "title": "Old", "updated_at": "2024-01-01T00:00:00Z", "user": {"login": "sam"}, "html_url": "u5"},
        {"number": 6, "title": "Fresh", "updated_at": "2024-05-30T00:00:00Z", "user": {"login": "kim"}, "html_url": "u6"},
    ])
    now = datetime(2024, 6, 1, tzinfo=timezone.utc)
    [sig] = prs.scan(ctx(), now=now)
    assert sig.summary == "#5 Old (by sam, idle 152d)" and "@" not in sig.summary
    assert sig.priority == "P2"


def test_remote_sources_skip_without_repo():
    c = Context(path=".", repo=None, gh=None)
    assert actions.scan(c) is None and prs.scan(c) is None
