import pytest

from issue_autopilot.github import GitHub
from issue_autopilot.sources import Context, advisories

OSV = "https://api.osv.dev/v1"
ALERTS = "https://api.github.com/repos/o/r/dependabot/alerts?state=open&per_page=100"


def vuln(vid, aliases=(), severity="HIGH", fixed=("2.31.0",)):
    return {"id": vid, "aliases": list(aliases), "summary": f"flaw {vid}",
            "database_specific": {"severity": severity},
            "affected": [{"package": {"name": "requests"},
                          "ranges": [{"events": [{"introduced": "0"}] + [{"fixed": f} for f in fixed]}]}]}


def test_osv_fallback_dedupes_aliases(tmp_path, http):
    (tmp_path / "requirements.txt").write_text("requests==2.25.0\nflask>=2\n")
    http.add("GET", ALERTS, {"message": "Dependabot alerts are disabled"}, status=403)
    http.add("POST", f"{OSV}/querybatch", {"results": [{"vulns": [{"id": "PYSEC-1"}, {"id": "GHSA-a"}, {"id": "GHSA-b"}]}]})
    http.add("GET", f"{OSV}/vulns/GHSA-a", vuln("GHSA-a", aliases=["PYSEC-1"], fixed=("2.0.0", "2.31.0")))
    http.add("GET", f"{OSV}/vulns/GHSA-b", vuln("GHSA-b", severity="MODERATE", fixed=()))
    http.add("GET", f"{OSV}/vulns/PYSEC-1", vuln("PYSEC-1", aliases=["GHSA-a"]))
    ctx = Context(path=str(tmp_path), repo="o/r", gh=GitHub("t"))
    sigs = advisories.scan(ctx)
    query = [c for c in http.calls if c[0] == "POST"][0][2]
    assert query == {"queries": [{"package": {"name": "requests", "ecosystem": "PyPI"}, "version": "2.25.0"}]}
    assert [(s.meta["advisory"], s.priority, s.meta["fixed"]) for s in sigs] == [
        ("GHSA-a", "P1", "2.31.0"), ("GHSA-b", "P2", None)]
    assert "no fix released" in sigs[1].summary and any("OSV.dev" in w for w in ctx.warnings)


def test_dependabot_alerts_preferred(http):
    http.add("GET", ALERTS, [{
        "html_url": "https://github.com/o/r/security/dependabot/1",
        "dependency": {"package": {"name": "lodash"}, "manifest_path": "web/package.json"},
        "security_advisory": {"ghsa_id": "GHSA-x", "summary": "Prototype pollution", "severity": "critical"},
        "security_vulnerability": {"vulnerable_version_range": "< 4.17.21",
                                   "first_patched_version": {"identifier": "4.17.21"}}}])
    [sig] = advisories.scan(Context(path=None, repo="o/r", gh=GitHub("t")))
    assert (sig.group, sig.priority) == ("web/package.json", "P1")
    assert sig.summary == "lodash < 4.17.21: GHSA-x Prototype pollution (critical severity; fixed in 4.17.21)"


def test_osv_outage_fails_source(tmp_path, http):
    import pytest
    (tmp_path / "requirements.txt").write_text("requests==2.25.0\n")
    http.add("POST", f"{OSV}/querybatch", {}, status=502)
    with pytest.raises(Exception):
        advisories.scan(Context(path=str(tmp_path), repo=None, gh=None))


PACKAGE_LOCK = """{"lockfileVersion": 3, "packages": {
  "": {"name": "app", "dependencies": {"lodash": "^4.17.0"}},
  "node_modules/lodash": {"version": "4.17.20"},
  "node_modules/a/node_modules/lodash": {"version": "4.17.20"},
  "node_modules/mine": {"link": true, "resolved": "../mine"}}}"""
POETRY_LOCK = """
[[package]]
name = "requests"
version = "2.25.0"

[[package]]
name = "local-lib"
version = "0.1.0"
[package.source]
type = "directory"
url = "../local-lib"
"""


def test_lockfiles_cover_ranges_and_dedupe(tmp_path, http):
    (tmp_path / "package.json").write_text('{"dependencies": {"lodash": "^4.17.0"}}')
    (tmp_path / "package-lock.json").write_text(PACKAGE_LOCK)
    (tmp_path / "requirements.txt").write_text("requests==2.25.0\n")
    (tmp_path / "poetry.lock").write_text(POETRY_LOCK)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package-lock.json").write_text("{not json")
    http.add("POST", f"{OSV}/querybatch", {"results": [{"vulns": [{"id": "GHSA-a"}]}, {}]})
    http.add("GET", f"{OSV}/vulns/GHSA-a", vuln("GHSA-a"))
    ctx = Context(path=str(tmp_path), repo=None, gh=None)
    sigs = advisories.scan(ctx)
    queries = [c for c in http.calls if c[0] == "POST"][0][2]["queries"]
    # manifest pin first; the duplicate lodash and the poetry copy of requests are dropped, local-lib skipped
    assert [(q["package"]["name"], q["version"]) for q in queries] == [("requests", "2.25.0"), ("lodash", "4.17.20")]
    assert [s.group for s in sigs] == ["requirements.txt"]
    assert any("web/package-lock.json" in w for w in ctx.warnings)


def test_npm_lock_v1():
    text = '{"lockfileVersion": 1, "dependencies": {"a": {"version": "1.0.0", "dependencies": {"b": {"version": "2.0.0"}}}}}'
    assert sorted(advisories._npm_lock(text)) == [("a", "1.0.0"), ("b", "2.0.0")]


YARN_CLASSIC = """# yarn lockfile v1


"@babel/code-frame@^7.0.0", "@babel/code-frame@^7.10.4":
  version "7.12.13"
  resolved "https://registry.yarnpkg.com/@babel/code-frame/-/code-frame-7.12.13.tgz"
  dependencies:
    "@babel/highlight" "^7.12.13"

lodash@^4.17.0:
  version "4.17.20"
"""
YARN_BERRY = """__metadata:
  version: 6

"app@workspace:.":
  version: 0.0.0-use.local
  resolution: "app@workspace:."

"minimist@npm:^1.2.0, minimist@npm:^1.2.5":
  version: 1.2.5
  resolution: "minimist@npm:1.2.5"
"""


def test_yarn_lock_classic_and_berry():
    assert advisories._yarn_lock(YARN_CLASSIC) == [("@babel/code-frame", "7.12.13"), ("lodash", "4.17.20")]
    assert advisories._yarn_lock(YARN_BERRY) == [("minimist", "1.2.5")]


@pytest.mark.parametrize("text", [
    "lockfileVersion: 5.4\npackages:\n  /lodash/4.17.20:\n    resolution: {integrity: x}\n"
    "  /@babel/core/7.12.3_react@17.0.2:\n    dev: true\n",
    "lockfileVersion: '6.0'\npackages:\n  /lodash@4.17.20:\n    resolution: {integrity: x}\n"
    "  /@babel/core@7.12.3(react@17.0.2):\n    dev: true\n  /mine@link:../mine:\n    dev: false\n",
    "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies: {}\npackages:\n  lodash@4.17.20:\n"
    "    resolution: {integrity: x}\n  '@babel/core@7.12.3':\n    resolution: {integrity: y}\n"
    "snapshots:\n  lodash@4.17.20: {}\n",
])
def test_pnpm_lock_v5_v6_v9(text):
    assert advisories._pnpm_lock(text) == [("lodash", "4.17.20"), ("@babel/core", "7.12.3")]


def test_yarn_and_pnpm_lockfiles_are_queried(tmp_path, http):
    (tmp_path / "yarn.lock").write_text(YARN_CLASSIC)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\npackages:\n  minimist@1.2.5:\n    x: y\n")
    http.add("POST", f"{OSV}/querybatch", {"results": [{}, {}, {}]})
    advisories.scan(Context(path=str(tmp_path), repo=None, gh=None))
    queries = [c for c in http.calls if c[0] == "POST"][0][2]["queries"]
    assert {(q["package"]["name"], q["version"], q["package"]["ecosystem"]) for q in queries} == {
        ("minimist", "1.2.5", "npm"), ("@babel/code-frame", "7.12.13", "npm"), ("lodash", "4.17.20", "npm")}
