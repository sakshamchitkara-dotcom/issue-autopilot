import pytest

from issue_autopilot.github import GitHub, GitHubError
from issue_autopilot.sources import Context, action_versions

API = "https://api.github.com"
WORKFLOW = """jobs:
  test:
    steps:
      - uses: actions/checkout@v3
      - uses: "actions/setup-python@v5"
      - uses: github/codeql-action/init@v2.1.0
      - uses: ./local-action
      - uses: docker://alpine:3
      - uses: actions/cache@0c45773b623bea8c8e75f6c82b208c3cf94ea4f9  # v4.0.2
      - uses: some/branch-pinned@main
      - uses: old/tag-only@v1
      - uses: gone/away@v1
"""


def test_flags_old_majors_and_skips_shas_branches_and_locals(tmp_path, http):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
    (tmp_path / "notes.yml").write_text("uses: actions/checkout@v1\n")  # not a workflow
    http.add("GET", f"{API}/repos/actions/checkout/releases/latest", {"tag_name": "v5.0.0"})
    http.add("GET", f"{API}/repos/actions/setup-python/releases/latest", {"tag_name": "v5.6.0"})
    http.add("GET", f"{API}/repos/github/codeql-action/releases/latest", {"tag_name": "v4.30.1"})
    http.add("GET", f"{API}/repos/old/tag-only/releases/latest", {"message": "Not Found"}, status=404)
    http.add("GET", f"{API}/repos/old/tag-only/tags?per_page=100", [{"name": "v2"}, {"name": "v2.0.1"}, {"name": "nightly"}])
    http.add("GET", f"{API}/repos/gone/away/releases/latest", {"message": "Not Found"}, status=404)
    http.add("GET", f"{API}/repos/gone/away/tags?per_page=100", {"message": "Not Found"}, status=404)
    ctx = Context(path=str(tmp_path), repo=None, gh=GitHub("t"))
    sigs = action_versions.scan(ctx)
    assert [(s.line, s.summary, s.priority) for s in sigs] == [
        (4, "actions/checkout@v3 → v5.0.0 (2 majors behind)", "P2"),
        (6, "github/codeql-action@v2.1.0 → v4.30.1 (2 majors behind)", "P2"),
        (11, "old/tag-only@v1 → v2.0.1 (1 major behind)", "P3"),
    ]
    assert {s.group for s in sigs} == {".github/workflows/ci.yml"}
    assert ctx.warnings == ["actions: gone/away has no releases or version tags; skipped"]
    assert sum(u.endswith("checkout/releases/latest") for _, u, _ in http.calls) == 1  # cached per action


def test_api_errors_fail_the_source(tmp_path, http):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("    - uses: actions/checkout@v3\n")
    http.add("GET", f"{API}/repos/actions/checkout/releases/latest", {"message": "rate limited"}, status=403)
    with pytest.raises(GitHubError):
        action_versions.scan(Context(path=str(tmp_path), repo=None, gh=GitHub("t")))
