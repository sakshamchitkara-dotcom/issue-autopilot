from issue_autopilot import sources
from issue_autopilot.sources import Context, iter_text_files, run_sources

from helpers import make_repo


def test_iter_text_files_skips_binary_big_vendored_and_excluded(tmp_path):
    root = make_repo(tmp_path, {"a.py": "x", "node_modules/m/i.js": "x", "tests/t.py": "x",
                                "big.txt": "y" * 2000, "img.png": "\0PNG"})
    assert [r for r, _ in iter_text_files(root, ["tests/*"], max_bytes=1000)] == ["a.py"]
    assert "big.txt" in [r for r, _ in iter_text_files(root, max_bytes=5000)]


def test_untracked_files_are_ignored_inside_git(tmp_path):
    root = make_repo(tmp_path, {"a.py": "x"})
    (tmp_path / "scratch.py").write_text("# TODO: local only\n")
    assert [r for r, _ in iter_text_files(root)] == ["a.py"]


def test_failing_source_is_a_warning_and_not_counted_as_ran(monkeypatch):
    def boom(ctx):
        raise RuntimeError("registry down")

    monkeypatch.setattr(sources, "_registry", lambda: {"ok": lambda c: [], "bad": boom, "na": lambda c: None})
    ctx = Context(path=None, repo=None, gh=None)
    signals, ran = run_sources(ctx)
    assert (signals, ran) == ([], ["ok"])  # "na" didn't apply, "bad" failed: neither may close issues
    assert ctx.warnings == ["source bad failed: registry down"]
