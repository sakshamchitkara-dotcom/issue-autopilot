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
