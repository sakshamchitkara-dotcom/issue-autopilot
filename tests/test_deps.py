import json

from issue_autopilot.sources import Context, deps


def test_parsers():
    assert deps.parse_requirements("requests==2.0.0\n# c\nflask>=2\nuvicorn[standard]==0.1 ; python_version>'3'\n") == [
        ("requests", "2.0.0"), ("uvicorn", "0.1")]
    pkg = json.dumps({"dependencies": {"react": "^17.0.2", "x": "latest"}, "devDependencies": {"@types/node": "~18.1.0"}})
    assert deps.parse_package_json(pkg) == [("react", "17.0.2"), ("@types/node", "18.1.0")]


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
        ("react 17.0.2 → 19.1.0 (major)", "P2", "package.json"),
        ("requests 2.0.0 → 2.32.3", "P3", "requirements.txt"),
    ]
    assert any("@scope/pkg" in w for w in ctx.warnings)
