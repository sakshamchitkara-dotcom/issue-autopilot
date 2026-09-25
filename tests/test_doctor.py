import json

from issue_autopilot import cli

from helpers import make_repo

API = "https://api.github.com"


def test_doctor_reports_access_and_fallbacks(tmp_path, http, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    root = make_repo(tmp_path, {"CODEOWNERS": "* @alice @org/team\n"})
    http.add("GET", f"{API}/user", {"login": "me"})
    http.add("GET", f"{API}/repos/o/r", {"permissions": {"push": False}})
    http.add("GET", f"{API}/repos/o/r/issues?state=all&per_page=1", [])
    http.add("GET", f"{API}/repos/o/r/dependabot/alerts?state=open&per_page=1", {"message": "disabled"}, status=403)
    http.add("GET", f"{API}/repos/o/r/actions/runs?per_page=1", {"workflow_runs": []})
    http.add("GET", f"{API}/repos/o/r/pulls?state=open&per_page=1", [])
    assert cli.main(["doctor", root, "--repo", "o/r", "--json"]) == 0
    checks = {c["check"]: (c["status"], c["detail"]) for c in json.loads(capsys.readouterr().out)}
    assert checks["token"] == ("ok", "from GITHUB_TOKEN") and checks["identity"] == ("ok", "me")
    assert checks["push access"][0] == "warn" and "--apply will be refused" in checks["push access"][1]
    assert checks["dependabot alerts"] == ("warn", "not readable (403); advisory falls back to OSV.dev")
    assert checks["CODEOWNERS"] == ("ok", "1 rule(s); team owners can't be assigned: @org/team")
    assert all(m == "GET" for m, _, _ in http.calls)


def test_doctor_fails_on_bad_credentials(tmp_path, http, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "bad")
    http.add("GET", f"{API}/user", {"message": "Bad credentials"}, status=401)
    http.add("GET", f"{API}/repos/o/r", {"message": "Bad credentials"}, status=401)
    http.add("GET", f"{API}/repos/o/r/issues?state=all&per_page=1", {"message": "Bad credentials"}, status=401)
    assert cli.main(["doctor", "o/r", "--sources", "todo"]) == 1
    out = capsys.readouterr().out
    assert "[fail] identity: GitHub API 401" in out and "[fail] issues: not readable (401)" in out
    assert "dependabot" not in out  # --sources todo: API sources aren't checked
