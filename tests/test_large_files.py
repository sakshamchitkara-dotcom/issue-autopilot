import os

import pytest

from issue_autopilot import cli, render, triage
from issue_autopilot.sources import Context, large_files

from helpers import make_repo

MB = 1024 * 1024


def write(root, rel, size, binary=True):
    with open(os.path.join(root, rel), "wb") as fh:
        fh.write((b"\0" if binary else b"x") * size)


def test_flags_big_binaries_only(tmp_path):
    root = str(tmp_path)
    for rel, size, binary in [("model.bin", 6 * MB, True), ("huge.zip", 60 * MB, True),
                              ("small.png", MB, True), ("data.json", 8 * MB, False)]:
        write(root, rel, size, binary)
    make_repo(tmp_path, {})
    write(root, "untracked.bin", 7 * MB)
    found = large_files.scan(Context(path=root, repo=None, gh=None))
    assert [(s.path, s.summary, s.priority) for s in found] == [
        ("huge.zip", "60.0 MB binary file", "P2"), ("model.bin", "6.0 MB binary file", "P3")]
    issue = render.render(triage.group(found)[0])
    assert issue.title == "Repo size: 2 large binary files committed" and "`huge.zip` 60.0 MB" in issue.body


def test_threshold_and_exclude_options(tmp_path):
    root = str(tmp_path)
    os.mkdir(os.path.join(root, "assets"))
    write(root, "a.bin", 2 * MB)
    write(root, "assets/b.bin", 2 * MB)
    make_repo(tmp_path, {})
    ctx = Context(path=root, repo=None, gh=None, options={"large_file_mb": 1.5, "exclude": ["assets/*"]})
    assert [s.path for s in large_files.scan(ctx)] == ["a.bin"]
    assert large_files.scan(Context(path=root, repo=None, gh=None)) == []  # default threshold is 5 MB


def test_threshold_must_be_positive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["scan", ".", "--large-file-mb", "0"])
    assert "must be greater than 0" in capsys.readouterr().err
