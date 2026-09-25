import os
import subprocess

import pytest

from issue_autopilot import owners
from issue_autopilot.github import GitHub
from issue_autopilot.models import Issue, Signal

from helpers import make_repo

CODEOWNERS = """
# comment
*                @fallback
/app/            @app-owner @org/team
*.md             @docs-writer
/web/**/*.js     @org/frontend
"""


def test_codeowners_last_match_wins_and_skips_teams(tmp_path):
    root = make_repo(tmp_path, {".github/CODEOWNERS": CODEOWNERS, "x": ""})
    rules = owners.load_codeowners(root)
    assert owners.codeowner(rules, "app/cache.py") == "app-owner"
    assert owners.codeowner(rules, "docs/guide.md") == "docs-writer"
    assert owners.codeowner(rules, "lib/x.py") == "fallback"
    assert owners.codeowner(rules, "web/src/a.js") is None  # team-only rule: nobody assignable
    assert owners.load_codeowners(str(tmp_path / "nope")) == []


def issue(path, line=1):
    return Issue("todo", path, [Signal("todo", path, "TODO: x", path=path, line=line)])


def test_blame_owner_via_noreply_email(tmp_path):
    root = make_repo(tmp_path, {"a.py": "# TODO: x\n"})
    with open(f"{root}/a.py", "a") as fh:
        fh.write("# TODO: y\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "Octo", "GIT_AUTHOR_EMAIL": "123+octo@users.noreply.github.com",
           "GIT_COMMITTER_NAME": "c", "GIT_COMMITTER_EMAIL": "c@example.com"}
    subprocess.run(["git", "-C", root, "commit", "-qam", "more"], check=True, env=env)
    assert owners.owner_for(root, issue("a.py", 2), []) == ("octo", "git blame")


def test_blame_owner_resolved_through_api(tmp_path, http):
    root = make_repo(tmp_path, {"a.py": "# TODO: x\n"})
    http.add("GET", "https://api.github.com/repos/o/r/commits/", {"author": {"login": "ada-gh"}})
    assert owners.owner_for(root, issue("a.py"), [], GitHub("t"), "o/r") == ("ada-gh", "git blame")
    assert owners.owner_for(root, issue("a.py"), []) is None  # no API, no noreply email
    rules = [("*.py", ["@py-owner"])]
    assert owners.owner_for(root, issue("a.py"), rules) == ("py-owner", "CODEOWNERS")


@pytest.mark.parametrize("pattern,path,expected", [
    ("*.js", "src/deep/a.js", True),
    ("/docs/*", "docs/a.md", True),
    ("/docs/*", "docs/sub/a.md", False),  # GitHub: dir/* covers direct children only
    ("src/*.py", "src/a.py", True),
    ("src/*.py", "src/pkg/a.py", False),  # * no longer crosses "/"
    ("src/*.py", "lib/src/a.py", False),  # a middle slash anchors to the root
    ("apps/", "x/apps/y/z.py", True),  # unanchored directory at any depth
    ("/build/logs/", "build/logs/a/b.log", True),
    ("docs/**/*.md", "docs/a/b/c.md", True),
    ("docs/**/*.md", "docs/c.md", True),
    ("**/logs", "deep/down/logs/x", True),
    ("a?c.py", "abc.py", True),
    ("a?c.py", "a/c.py", False),
])
def test_codeowners_glob_semantics(pattern, path, expected):
    assert owners.matches(pattern, path) is expected
