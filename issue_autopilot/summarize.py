"""Optional Claude pass that rewrites an issue's title and intro. Falls back to templates."""
from __future__ import annotations

import json
import os
import sys

from .models import Issue
from .render import render, signals_md, title_for

MODEL = "claude-opus-5-5"
SYSTEM = (
    "You write GitHub issues for an engineering team from automatically detected repo signals. "
    "Given the signals, produce a specific, actionable title (under 90 characters, no emoji, no "
    "trailing period) and a short intro in GitHub Markdown: what is wrong, why it matters, and a "
    "concrete suggested next step. Base every claim on the signals; do not invent files, versions "
    "or causes. Never reveal or reconstruct redacted secret values. Do not @mention anyone. "
    "The signal list itself is appended after your intro automatically, so do not repeat it."
)
SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "intro": {"type": "string"}},
    "required": ["title", "intro"],
    "additionalProperties": False,
}


def make_client():
    """Return an Anthropic client when ANTHROPIC_API_KEY is set and the SDK is installed."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        print("warning: ANTHROPIC_API_KEY set but `anthropic` not installed; using templates", file=sys.stderr)
        return None
    return anthropic.Anthropic()


def polish(issue: Issue, client) -> Issue:
    """Rewrite title/intro with Claude; any failure keeps the template version."""
    render(issue)  # template first, so we always have a valid issue
    if client is None:
        return issue
    prompt = (
        f"Signal kind: {issue.kind}\nGroup: {issue.group}\nDefault title: {title_for(issue)}\n\n"
        f"{signals_md(issue)[:20_000]}"
    )
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason != "end_turn":  # refusal / max_tokens: keep template
            raise ValueError(f"stop_reason={resp.stop_reason}")
        data = json.loads(next(b.text for b in resp.content if b.type == "text"))
        return render(issue, title=data["title"], intro=data["intro"])
    except Exception as e:  # network, auth, rate limit, bad JSON: never block filing
        print(f"warning: Claude summarizer failed for {issue.fingerprint} ({e}); using template", file=sys.stderr)
        return issue
