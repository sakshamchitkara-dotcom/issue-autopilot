"""Minimal GitHub REST client on top of urllib (no third-party deps)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
USER_AGENT = "issue-autopilot/0.2"


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def get_token() -> str | None:
    """GITHUB_TOKEN wins; otherwise ask the gh CLI."""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok.strip()
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def http_get_json(url: str, headers: dict | None = None, timeout: float = 20):
    """Plain unauthenticated GET for public registries (PyPI, npm)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_post_json(url: str, body: dict, timeout: float = 30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class GitHub:
    def __init__(self, token: str | None, api: str = API):
        self.token = token
        self.api = api.rstrip("/")

    def request(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        url = path if path.startswith("http") else f"{self.api}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            # Unredirected: log downloads redirect to blob storage, which must not see our token.
            req.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                link = resp.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            raise GitHubError(e.code, msg) from None
        if raw:
            return payload.decode(errors="replace"), link
        return (json.loads(payload) if payload else None), link

    def get(self, path: str):
        return self.request("GET", path)[0]

    def paginate(self, path: str, limit: int = 1000) -> list:
        """Follow Link: rel=next until exhausted or `limit` items collected."""
        items: list = []
        url: str | None = path + ("&" if "?" in path else "?") + "per_page=100"
        while url and len(items) < limit:
            page, link = self.request("GET", url)
            if isinstance(page, dict):  # e.g. {"workflow_runs": [...]}
                page = next((v for v in page.values() if isinstance(v, list)), [])
            items.extend(page)
            m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
            url = m.group(1) if m else None
        return items[:limit]

    def text(self, path: str) -> str:
        return self.request("GET", path, raw=True)[0]

    # --- convenience wrappers -------------------------------------------------
    def whoami(self) -> str:
        return self.get("/user")["login"]

    def repo(self, full_name: str) -> dict:
        return self.get(f"/repos/{full_name}")

    def can_push(self, full_name: str) -> bool:
        perms = self.repo(full_name).get("permissions") or {}
        return bool(perms.get("push") or perms.get("admin") or perms.get("maintain"))

    def issues(self, full_name: str, state: str = "open") -> list[dict]:
        issues = self.paginate(f"/repos/{full_name}/issues?state={state}")
        return [i for i in issues if "pull_request" not in i]

    def open_issues(self, full_name: str) -> list[dict]:
        return self.issues(full_name, "open")

    def update_issue(self, full_name: str, number: int, **fields) -> dict:
        """PATCH any of title/body/labels/state/assignees."""
        return self.request("PATCH", f"/repos/{full_name}/issues/{number}", fields)[0]

    def create_issue(self, full_name: str, title: str, body: str, labels: list[str],
                     assignees: list[str] | None = None) -> dict:
        payload = {"title": title, "body": body, "labels": labels}
        if assignees:
            payload["assignees"] = assignees
        return self.request("POST", f"/repos/{full_name}/issues", payload)[0]

    def comment(self, full_name: str, number: int, body: str) -> None:
        self.request("POST", f"/repos/{full_name}/issues/{number}/comments", {"body": body})

    def close_issue(self, full_name: str, number: int) -> None:
        self.update_issue(full_name, number, state="closed", state_reason="completed")

    def add_labels(self, full_name: str, number: int, labels: list[str]) -> None:
        self.request("POST", f"/repos/{full_name}/issues/{number}/labels", {"labels": labels})


def parse_repo_slug(value: str) -> str | None:
    """Return owner/repo for 'owner/repo', a github URL, or a git remote; else None."""
    m = re.match(r"^(?:https://github\.com/|git@github\.com:)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", value.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def repo_from_checkout(path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_repo_slug(out.stdout) if out.returncode == 0 else None
