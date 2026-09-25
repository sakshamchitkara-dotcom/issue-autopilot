import time

from issue_autopilot.sources import Context
from issue_autopilot.sources import todos
from issue_autopilot.sources.blame import annotate

from helpers import make_repo

SRC = """import os
# TODO: handle retries
x = 1  # FIXME(bob): off by one
TODO_LIST = []  # not a comment tag
// HACK work around upstream bug
"""


def test_finds_tags_with_blame(tmp_path):
    root = make_repo(tmp_path, {"app.py": SRC, "node_modules/x.js": "// TODO skip me"})
    sigs = todos.scan(Context(path=root, repo=None, gh=None))
    assert [(s.line, s.meta["tag"]) for s in sigs] == [(2, "TODO"), (3, "FIXME"), (5, "HACK")]
    assert sigs[0].summary == "TODO: handle retries" and sigs[0].priority == "P3"
    assert sigs[1].meta["owner"] == "bob" and sigs[1].priority == "P2"
    assert sigs[0].meta["author"] == "Ada" and sigs[0].meta["age_days"] == 0
    assert {s.group for s in sigs} == {"app.py"}


def test_old_todo_is_bumped(tmp_path):
    root = make_repo(tmp_path, {"a.py": "# TODO: ancient\n"})
    sigs = todos.scan(Context(path=root, repo=None, gh=None))
    annotate(root, sigs, now=time.time() + 400 * 86400)  # pretend a year+ has passed
    assert sigs[0].meta["age_days"] >= 399 and sigs[0].priority == "P2"


def test_works_outside_git(tmp_path):
    (tmp_path / "b.js").write_text("/* XXX: nope */\n")
    sigs = todos.scan(Context(path=str(tmp_path), repo=None, gh=None))
    assert sigs[0].summary == "XXX: nope" and "author" not in sigs[0].meta


def test_skips_without_path():
    assert todos.scan(Context(path=None, repo="o/r", gh=None)) is None


def test_exclude_globs(tmp_path):
    root = make_repo(tmp_path, {"src/a.py": "# TODO: keep\n", "tests/fixtures/b.py": "# TODO: drop\n"})
    sigs = todos.scan(Context(path=root, repo=None, gh=None, options={"exclude": ["tests/*"]}))
    assert [s.path for s in sigs] == ["src/a.py"]
