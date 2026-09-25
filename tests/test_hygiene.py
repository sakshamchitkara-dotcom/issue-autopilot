from issue_autopilot import render, triage
from issue_autopilot.github import GitHub
from issue_autopilot.sources import Context, hygiene

from helpers import make_repo


def summaries(ctx):
    return [s.meta["file"] for s in hygiene.scan(ctx)]


def test_missing_license_and_security_policy(tmp_path):
    ctx = Context(path=make_repo(tmp_path, {"a.py": "x"}), repo=None, gh=None)
    assert summaries(ctx) == ["LICENSE", "SECURITY.md"]
    issue = render.render(triage.group(hygiene.scan(ctx))[0])
    assert issue.title == "Repo hygiene: missing LICENSE and SECURITY.md"
    assert issue.labels == ["autopilot", "repo-hygiene", "P2"]


def test_recognised_names_and_locations(tmp_path):
    for i, files in enumerate([{"LICENSE": "", "SECURITY.md": ""}, {"license.txt": "", ".github/SECURITY.md": ""},
                               {"LICENSE-MIT": "", "docs/security.rst": ""}, {"COPYING": "", "docs/SECURITY.md": ""}]):
        (tmp_path / str(i)).mkdir()
        assert summaries(Context(path=make_repo(tmp_path / str(i), files), repo=None, gh=None)) == []


def test_untracked_or_nested_files_dont_count(tmp_path):
    root = make_repo(tmp_path, {"pkg/LICENSE": "", "src/SECURITY.md": ""})
    (tmp_path / "LICENSE").write_text("not committed")
    assert summaries(Context(path=root, repo=None, gh=None)) == ["LICENSE", "SECURITY.md"]


def test_private_repos_are_not_asked_for_a_license(tmp_path, http):
    root = make_repo(tmp_path, {"a.py": "x"})
    http.add("GET", "https://api.github.com/repos/o/private", {"private": True})
    http.add("GET", "https://api.github.com/repos/o/public", {"private": False})
    http.add("GET", "https://api.github.com/repos/o/gone", {"message": "Not Found"}, status=404)
    assert summaries(Context(path=root, repo="o/private", gh=GitHub("t"))) == ["SECURITY.md"]
    assert summaries(Context(path=root, repo="o/public", gh=GitHub("t"))) == ["LICENSE", "SECURITY.md"]
    assert summaries(Context(path=root, repo="o/gone", gh=GitHub("t"))) == ["LICENSE", "SECURITY.md"]
