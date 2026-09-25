import pytest

from issue_autopilot.github import GitHub, GitHubError, parse_repo_slug

API = "https://api.github.com"


def test_paginate_follows_link_header(http):
    http.add("GET", f"{API}/repos/o/r/issues?state=open&per_page=100", [{"number": 1}],
             headers={"Link": f'<{API}/page2>; rel="next"'})
    http.add("GET", f"{API}/page2", [{"number": 2}, {"number": 3, "pull_request": {}}])
    issues = GitHub("t").open_issues("o/r")
    assert [i["number"] for i in issues] == [1, 2]  # PRs filtered out


def test_paginate_unwraps_dict_pages(http):
    http.add("GET", f"{API}/x?per_page=100", {"total_count": 1, "workflow_runs": [{"id": 9}]})
    assert GitHub("t").paginate("/x") == [{"id": 9}]


def test_can_push(http):
    http.add("GET", f"{API}/repos/o/yes", {"permissions": {"push": True}})
    http.add("GET", f"{API}/repos/o/no", {"permissions": {"push": False, "pull": True}})
    gh = GitHub("t")
    assert gh.can_push("o/yes") and not gh.can_push("o/no")


def test_http_error_raises(http):
    http.add("GET", f"{API}/user", {"message": "Bad credentials"}, status=401)
    with pytest.raises(GitHubError) as e:
        GitHub("bad").whoami()
    assert e.value.status == 401


def test_auth_header_not_forwarded_on_redirect(http):
    http.add("GET", f"{API}/user", {"login": "me"})
    GitHub("secret").whoami()
    req = http.last_request
    # urllib copies req.headers onto redirects but not unredirected_hdrs
    assert req.unredirected_hdrs["Authorization"] == "Bearer secret"
    assert "Authorization" not in req.headers


@pytest.mark.parametrize("value,expected", [
    ("octo/cat", "octo/cat"),
    ("https://github.com/octo/cat.git", "octo/cat"),
    ("git@github.com:octo/cat.git\n", "octo/cat"),
    ("not a repo", None),
])
def test_parse_repo_slug(value, expected):
    assert parse_repo_slug(value) == expected


def test_issue_listing_and_edits(http):
    from issue_autopilot.github import GitHub
    gh = GitHub("t")
    http.add("GET", "https://api.github.com/repos/o/r/issues?state=all&per_page=100",
             [{"number": 1}, {"number": 2, "pull_request": {}}])
    http.add("PATCH", "https://api.github.com/repos/o/r/issues/1", {"number": 1})
    http.add("POST", "https://api.github.com/repos/o/r/issues/1/labels", [])
    assert [i["number"] for i in gh.issues("o/r", "all")] == [1]
    gh.update_issue("o/r", 1, title="t", state="open")
    gh.add_labels("o/r", 1, ["autopilot:resolved"])
    assert http.calls[1][2] == {"title": "t", "state": "open"}
    assert http.calls[2][2] == {"labels": ["autopilot:resolved"]}


def test_write_access_for_users_and_installation_tokens(http):
    http.add("GET", f"{API}/user", {"login": "me"})
    http.add("GET", f"{API}/repos/o/r", {"permissions": {"push": True}})
    assert GitHub("t").write_access("o/r") == ("me", True)

    http.add("GET", f"{API}/user", {"message": "Resource not accessible by integration"}, status=403)
    http.add("GET", f"{API}/installation/repositories?per_page=100",
             {"total_count": 1, "repositories": [{"full_name": "O/R"}]})
    assert GitHub("t").write_access("o/r") == ("installation token", True)
    assert GitHub("t").write_access("psf/requests") == ("installation token", False)
