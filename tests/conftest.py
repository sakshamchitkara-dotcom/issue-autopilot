import io
import json
import urllib.error
import urllib.request

import pytest


class FakeResponse(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHTTP:
    """Route table for urllib: exact 'METHOD url' keys, falling back to longest prefix match."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, method, url, body=None, status=200, headers=None):
        self.routes[f"{method} {url}"] = (status, body, headers or {})

    def __call__(self, req, timeout=None):
        method = req.get_method()
        url = req.full_url
        body = json.loads(req.data) if req.data else None
        self.calls.append((method, url, body))
        self.last_request = req
        key = f"{method} {url}"
        match = self.routes.get(key)
        if match is None:
            prefixes = [k for k in self.routes if key.startswith(k)]
            if not prefixes:
                raise AssertionError(f"unexpected request: {key}")
            match = self.routes[max(prefixes, key=len)]
        status, payload, headers = match
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(json.dumps(payload).encode()))
        return FakeResponse(payload if payload is not None else b"", headers)

    def posted(self, method="POST"):
        return [c for c in self.calls if c[0] == method]


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake
