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
    ("==1.2.*", "1.2.7", False), ("1.x", "2.0.0", True), ("^1 || ^2", "3.0.0", False), ("!=1.5", "2.0", False),
])
def test_range_semantics(spec, latest, expected):
    assert deps.behind(spec, latest) is expected


def test_scan_flags_outdated(tmp_path, http):
    (tmp_path / "requirements.txt").write_text("requests==2.0.0\nidna==3.7\n")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^17.0.2", "@scope/pkg": "1.0.0"}}))
    http.add("GET", "https://pypi.org/pypi/requests/json", {"info": {"version": "2.32.3"}})
    http.add("GET", "https://pypi.org/pypi/idna/json", {"info": {"version": "3.7"}})
    http.add("GET", "https://registry.npmjs.org/react/latest", {"version": "19.1.0"})
    http.add("GET", "https://registry.npmjs.org/@scope/pkg/latest", {"message": "nope"}, status=404)
    ctx = Context(path=str(tmp_path), repo=None, gh=None)
    sigs = sorted(deps.scan(ctx), key=lambda s: s.summary)
    assert [(s.summary, s.priority, s.group) for s in sigs] == [
        ("react ^17.0.2 → 19.1.0 (major)", "P2", "package.json"),
        ("requests 2.0.0 → 2.32.3", "P3", "requirements.txt"),
    ]
    assert any("@scope/pkg" in w for w in ctx.warnings)


def test_registry_outage_fails_source(tmp_path, http):
    (tmp_path / "requirements.txt").write_text("requests==2.0.0\n")
    http.add("GET", "https://pypi.org/pypi/requests/json", {}, status=503)
    with pytest.raises(RuntimeError, match="registry lookup"):
        deps.scan(Context(path=str(tmp_path), repo=None, gh=None))
