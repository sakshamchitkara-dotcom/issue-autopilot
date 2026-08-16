import json

import pytest

from issue_autopilot.sources import Context, deps


def test_parsers():
    text = "requests==2.0.0\n# c\nflask >= 2, <3\nuvicorn[standard]~=0.1 ; python_version>'3'\nbare\nx @ https://e/x.whl\n"
    assert deps.parse_requirements(text) == [("requests", "==2.0.0"), ("flask", ">= 2, <3"), ("uvicorn", "~=0.1")]
    pkg = json.dumps({"dependencies": {"react": "^17.0.2", "x": "latest", "g": "github:a/b"},
                      "devDependencies": {"@types/node": "~18.1.0", "y": "1.x"}})
    assert deps.parse_package_json(pkg) == [("react", "^17.0.2"), ("@types/node", "~18.1.0"), ("y", "1.x")]


@pytest.mark.parametrize("spec,latest,expected", [
    ("==2.0.0", "2.0.1", True), ("==2.0.0", "2.0.0", False), ("1.0.0", "1.0.1", True),
    (">=1.0", "9.0", False), (">=1,<2", "1.9", False), (">=1,<2", "2.0", True), ("<=1.5", "1.5", False),
    ("~=1.4", "1.9", False), ("~=1.4", "2.0", True), ("~=1.4.2", "1.5.0", True),
    ("^1.2.3", "1.9.0", False), ("^1.2.3", "2.0.0", True), ("^0.2.3", "0.3.0", True), ("^0.0.3", "0.0.4", True),
    ("~1.2.3", "1.2.9", False), ("~1.2.3", "1.3.0", True), ("~1", "1.9", False), ("~1", "2.0", True),
    ("==1.2.*", "1.2.7", False), ("1.x", "2.0.0", True), ("^1 || ^2", "2.5.0", False), ("^1 || ^2", "3.0.0", True), ("1.2.3 - 2.3.4", "2.3.4", False),
    ("1.2.3 - 2.3.4", "2.3.5", True), ("1 - 2", "2.9.9", False), ("1 - 2", "3.0.0", True), ("^1 || foo", "9.0", False),
    ("!=1.5", "2.0", False),
])
def test_range_semantics(spec, latest, expected):
    assert deps.behind(spec, latest) is expected


def test_scan_flags_outdated(tmp_path, http):
    (tmp_path / "requirements.txt").write_text("requests==2.0.0\nidna==3.7\nflask>=1.0\nclick>=8.0\n")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^17.0.2", "@scope/pkg": "1.0.0"}}))
    http.add("GET", "https://pypi.org/pypi/requests/json", {"info": {"version": "2.32.3"}})
    http.add("GET", "https://pypi.org/pypi/idna/json", {"info": {"version": "3.7"}})
    http.add("GET", "https://pypi.org/pypi/flask/json", {"info": {"version": "3.1.0"}})
    http.add("GET", "https://pypi.org/pypi/click/json", {"info": {"version": "8.2.0"}})
    http.add("GET", "https://registry.npmjs.org/react/latest", {"version": "19.1.0"})
    http.add("GET", "https://registry.npmjs.org/@scope/pkg/latest", {"message": "nope"}, status=404)
    ctx = Context(path=str(tmp_path), repo=None, gh=None)
    sigs = sorted(deps.scan(ctx), key=lambda s: s.summary)
    assert [(s.summary, s.priority, s.group) for s in sigs] == [
        ("flask >=1.0 → 3.1.0 (allowed, but the floor is 2 majors behind)", "P3", "requirements.txt"),
        ("react ^17.0.2 → 19.1.0 (2 majors behind)", "P2", "package.json"),
        ("requests 2.0.0 → 2.32.3", "P3", "requirements.txt"),
    ]
    assert any("@scope/pkg" in w for w in ctx.warnings)


def test_registry_outage_fails_source(tmp_path, http):
    (tmp_path / "requirements.txt").write_text("requests==2.0.0\n")
    http.add("GET", "https://pypi.org/pypi/requests/json", {}, status=503)
    with pytest.raises(RuntimeError, match="registry lookup"):
        deps.scan(Context(path=str(tmp_path), repo=None, gh=None))


PYPROJECT = """
[project]
name = "demo"
dependencies = ["httpx>=0.20,<0.21", "rich ~= 10.0", "click"]

[tool.poetry.dependencies]
python = "^3.10"
django = "^3.2"
pydantic = { version = "~1.8", extras = ["email"] }
local = { path = "../local" }

[tool.poetry.group.dev.dependencies]
pytest = "7.0.0"
"""


def test_pyproject_project_and_poetry():
    assert deps.parse_pyproject(PYPROJECT) == [
        ("httpx", ">=0.20,<0.21"), ("rich", "~= 10.0"),
        ("django", "^3.2"), ("pydantic", "~1.8"), ("pytest", "7.0.0")]
    assert deps.parse_pyproject("not [toml") == []


def test_scan_reads_pyproject(tmp_path, http):
    (tmp_path / "pyproject.toml").write_text('[tool.poetry.dependencies]\npython = "^3.10"\ndjango = "^3.2"\n')
    http.add("GET", "https://pypi.org/pypi/django/json", {"info": {"version": "5.2.1"}})
    sigs = deps.scan(Context(path=str(tmp_path), repo=None, gh=None))
    assert [(s.group, s.summary) for s in sigs] == [("pyproject.toml", "django ^3.2 → 5.2.1 (2 majors behind)")]


def test_majors_behind():
    assert deps.majors_behind("==1.1.4", "3.1.0") == 2
    assert deps.majors_behind("^0.4", "1.0.0") == 1
    assert deps.majors_behind("~=2.1", "2.9") == 0 and deps.majors_behind("||", "2") == 0


def test_or_ranges_use_the_newest_alternative_for_lag():
    assert deps.majors_behind("^1 || ^2", "4.0.0") == 2
    assert len(deps.bounds("^1 || ^2")) == 2 and deps.bounds("^1 || whatever") == []


@pytest.mark.parametrize("raw,expected", [
    ("1.0rc1", (1, 0)), ("2.0.0.dev3", (2, 0, 0)), ("1.0.post1", (1, 0)), ("1.2.3-beta.1", (1, 2, 3)),
    ("v4", (4,)), ("4.17.21+build.5", (4, 17, 21)), ("latest", ()),
])
def test_version_tuple_ignores_prerelease_suffixes(raw, expected):
    assert deps.version_tuple(raw) == expected


def test_advisory_fix_older_than_the_installed_version_is_ignored():
    from issue_autopilot.sources import advisories
    vuln = {"affected": [{"package": {"name": "x"}, "ranges": [{"events": [{"fixed": "1.0rc1"}, {"fixed": "1.0.2"}]}]}]}
    # 1.0rc1 used to parse as 1.0.1 and was reported as the fix for 1.0.0
    assert advisories._fixed_after(vuln, "x", "1.0.0") == "1.0.2"
