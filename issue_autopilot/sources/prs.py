"""Open pull requests with no activity for N days, collected into one issue."""
from __future__ import annotations

from datetime import datetime, timezone

from ..models import Signal
from . import Context

DEFAULT_DAYS = 30


def scan(ctx: Context, now: datetime | None = None) -> list[Signal] | None:
    if not (ctx.repo and ctx.gh):
        return None
    days = int(ctx.options.get("stale_days", DEFAULT_DAYS))
    now = now or datetime.now(timezone.utc)
    signals = []
    for pr in ctx.gh.paginate(f"/repos/{ctx.repo}/pulls?state=open&sort=updated&direction=asc"):
        updated = datetime.fromisoformat(pr["updated_at"].replace("Z", "+00:00"))
        idle = (now - updated).days
        if idle < days:
            continue
        draft = " [draft]" if pr.get("draft") else ""
        signals.append(
            Signal(
                kind="stale-pr",
                group="open-prs",
                # No "@" before the login: we never want to ping people from a bot issue.
                summary=f"#{pr['number']} {pr['title']}{draft} (by {pr['user']['login']}, idle {idle}d)",
                priority="P2" if idle >= days * 3 else "P3",
                meta={"number": pr["number"], "url": pr["html_url"], "idle_days": idle},
            )
        )
    return signals
