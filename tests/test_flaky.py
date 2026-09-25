from issue_autopilot.github import GitHub
from issue_autopilot.sources import Context, flaky

API = "https://api.github.com/repos/o/r/actions"


def run(id, sha, conclusion, attempt=1, wf=1):
    return {"id": id, "workflow_id": wf, "head_sha": sha, "conclusion": conclusion, "run_attempt": attempt,
            "name": "tests", "path": ".github/workflows/tests.yml"}


def jobs(*failed, ok=("lint",)):
    return {"jobs": [{"name": n, "conclusion": "failure"} for n in failed] +
                    [{"name": n, "conclusion": "success"} for n in ok]}


def test_reruns_and_disagreeing_runs_on_one_commit(http):
    http.add("GET", f"{API}/runs?status=completed&per_page=100", {"workflow_runs": [
        run(1, "aaaaaaa1", "success", attempt=2),   # rerun passed: attempt 1 failed `pytest (3.10)`
        run(2, "bbbbbbb2", "success", attempt=3),   # attempt 2 failed `pytest (3.10)` too
        run(3, "ccccccc3", "failure"), run(4, "ccccccc3", "success"),  # same commit, two verdicts
        run(5, "ddddddd4", "failure"),              # plain failure: not flaky
        run(6, "eeeeeee5", "success", attempt=2),   # only one flip for `e2e`: below the threshold
    ]})
    http.add("GET", f"{API}/runs/1/attempts/1/jobs", jobs("pytest (3.10)"))
    http.add("GET", f"{API}/runs/2/attempts/2/jobs", jobs("pytest (3.10)"))
    http.add("GET", f"{API}/runs/3/jobs?filter=latest", jobs("pytest (3.10)"))
    http.add("GET", f"{API}/runs/6/attempts/1/jobs", jobs("e2e"))
    [sig] = flaky.scan(Context(path=None, repo="o/r", gh=GitHub("t")))
    assert sig.group == ".github/workflows/tests.yml" and sig.priority == "P2"
    assert sig.summary == "job `pytest (3.10)` in 'tests' failed, then passed on the same commit"
    assert sig.meta["flips"] == 3 and "commits aaaaaaa, bbbbbbb, ccccccc" in sig.detail
    assert not any("/runs/5/" in u for _, u, _ in http.calls)


def test_lookup_cap_warns(http, monkeypatch):
    monkeypatch.setattr(flaky, "MAX_LOOKUPS", 1)
    http.add("GET", f"{API}/runs?status=completed&per_page=100",
             {"workflow_runs": [run(1, "a", "success", 2), run(2, "b", "success", 2)]})
    http.add("GET", f"{API}/runs/1/attempts/1/jobs", jobs("x"))
    ctx = Context(path=None, repo="o/r", gh=GitHub("t"))
    assert flaky.scan(ctx) == [] and ctx.warnings == ["flaky: checked 1 of 2 retried runs"]
    assert flaky.scan(Context(path=".", repo=None, gh=None)) is None
