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
    p = triage.plan(issues, existing, cap=1)
    assert p.unchanged == [issues[0]] and p.create == [issues[1]] and p.over == [issues[2]]


def test_plan_flags_changed_and_legacy_groups():
    issues = [render.render(i) for i in triage.group(sigs())]
    changed = triage.group(sigs() + [Signal("todo", "a.py", "TODO: new", "P3", "a.py", 7)])
    legacy = issues[2].body.replace(f" sig={issues[2].digest}", "")
    existing = triage.index_existing([{"number": 1, "body": issues[0].body}, {"number": 2, "body": issues[1].body},
                                      {"number": 3, "body": legacy}])
    p = triage.plan(changed, existing, cap=10)
    assert [i.group for i in p.update] == ["a.py", "b.py"] and [i.group for i in p.unchanged] == ["c.py"]


def test_changelog_lists_added_and_removed_ignoring_age():
    old = "- [P3] `a.py:1` TODO: x (Ada, 3d old)\n- [P3] `a.py:2` TODO: y (Ada, 3d old)\n"
    new = "- [P3] `a.py:1` TODO: x (Ada, 9d old)\n- [P2] `a.py:5` FIXME: z @bob\n"
    note = render.changelog(old, new)
    assert "**Added**\n- [P2] `a.py:5` FIXME: z @\u200bbob" in note
    assert "**Removed**\n- [P3] `a.py:2` TODO: y" in note and "TODO: x" not in note


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


def test_digest_tracks_signal_changes_not_blame_age():
    base = triage.group(sigs())[1]  # todo a.py
    assert triage.marker_digest(render.render(base).body) == base.digest
    aged = triage.group(sigs())[1]
    aged.signals[0].meta.update(author="Ada", age_days=400)
    assert aged.digest == base.digest
    more = triage.group(sigs() + [Signal("todo", "a.py", "TODO: new", "P3", "a.py", 7)])[1]
    assert more.digest != base.digest and more.fingerprint == base.fingerprint


def test_legacy_marker_still_parses():
    body = "x\n<!-- issue-autopilot fp=0123456789abcdef kind=todo -->"
    assert triage.parse_marker(body) == ("0123456789abcdef", "todo") and triage.marker_digest(body) is None


def test_index_prefers_open_then_newest():
    body = render.render(triage.group(sigs())[0]).body
    idx = triage.index_existing([{"number": 1, "body": body, "state": "open"},
                                 {"number": 5, "body": body, "state": "closed", "labels": []}])
    assert idx[triage.group(sigs())[0].fingerprint]["number"] == 1
    assert triage.closed_by_human({"state": "closed", "labels": [{"name": "autopilot:resolved"}]}) is False
    assert triage.closed_by_human({"state": "closed", "labels": []}) is True
