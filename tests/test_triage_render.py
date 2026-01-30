import json
from types import SimpleNamespace

from issue_autopilot import render, summarize, triage
from issue_autopilot.models import Signal


def sigs():
    return [
        Signal("todo", "a.py", "TODO: x", "P3", "a.py", 3, meta={"tag": "TODO"}),
        Signal("todo", "a.py", "FIXME: y", "P2", "a.py", 1, meta={"tag": "FIXME"}),
        Signal("secret", "c.py", "possible aws-access-key (AKIA…(20 chars))", "P1", "c.py", 2),
        Signal("todo", "b.py", "HACK: z @octocat", "P2", "b.py", 9, meta={"tag": "HACK"}),
    ]


def test_grouping_priority_labels():
    issues = triage.group(sigs())
    assert [(i.kind, i.group, i.priority) for i in issues] == [
        ("secret", "c.py", "P1"), ("todo", "a.py", "P2"), ("todo", "b.py", "P2")]
    assert issues[1].labels == ["autopilot", "tech-debt", "P2"]
    assert [s.line for s in issues[1].signals] == [1, 3]  # most urgent first


def test_fingerprint_stable_and_marker_roundtrip():
    a, b = triage.group(sigs()), triage.group(list(reversed(sigs())))
    assert {i.fingerprint for i in a} == {i.fingerprint for i in b}
    issue = render.render(a[0])
    assert triage.parse_marker(issue.body) == (a[0].fingerprint, "secret")


def test_render_neutralizes_mentions():
    issue = render.render([i for i in triage.group(sigs()) if i.group == "b.py"][0])
    assert "@octocat" not in issue.body and "@​octocat" in issue.body
    assert issue.title == "Tech debt: 1 HACK comment in b.py"


def test_plan_dedupes_and_caps():
    issues = [render.render(i) for i in triage.group(sigs())]
    existing = triage.index_existing([{"number": 7, "body": issues[0].body}, {"number": 8, "body": "unrelated"}])
    new, dupes, over = triage.plan(issues, existing, cap=1)
    assert dupes == [issues[0]] and new == [issues[1]] and over == [issues[2]]


def test_resolved_only_for_scanned_kinds():
    issues = [render.render(i) for i in triage.group(sigs())]
    existing = triage.index_existing([{"number": n, "body": i.body} for n, i in enumerate(issues)])
    # secret signal vanished; todo source didn't run this time
    gone = triage.resolved(existing, [issues[1], issues[2]], scanned_kinds=["secret"])
    assert [g["number"] for g in gone] == [0]
    assert triage.resolved(existing, [], scanned_kinds=["deps"]) == []


class FakeClient:
    def __init__(self, reply=None, stop="end_turn", exc=None):
        self.reply, self.stop, self.exc, self.kwargs = reply, stop, exc, None
        self.messages = self

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.exc:
            raise self.exc
        return SimpleNamespace(stop_reason=self.stop,
                               content=[SimpleNamespace(type="text", text=json.dumps(self.reply))])


def test_summarizer_uses_claude_output_but_keeps_marker():
    issue = triage.group(sigs())[0]
    client = FakeClient({"title": "Rotate leaked AWS key in c.py", "intro": "An AWS key appears in c.py."})
    summarize.polish(issue, client)
    assert client.kwargs["model"] == "claude-opus-5-5"
    assert client.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert issue.title == "Rotate leaked AWS key in c.py"
    assert issue.body.startswith("An AWS key appears") and triage.parse_marker(issue.body)


def test_summarizer_falls_back_to_template():
    for client in (None, FakeClient(exc=RuntimeError("boom")), FakeClient({"title": "x"}, stop="refusal")):
        issue = summarize.polish(triage.group(sigs())[0], client)
        assert issue.title == "Security: possible hardcoded secret in c.py"


def test_make_client_requires_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert summarize.make_client() is None
