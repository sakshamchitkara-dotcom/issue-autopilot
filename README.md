# issue-autopilot

Turns signals already sitting in a repository into deduplicated GitHub issues. It keeps those issues up to date as the signals change and closes them once the signals are gone. It leaves alone any issue a human has closed.

| Source     | What it finds | Grouped into one issue per |
|------------|---------------|----------------------------|
| `todo`     | `TODO` / `FIXME` / `HACK` / `XXX` comments, with the `git blame` author and age | file |
| `secret`   | Hardcoded credentials (AWS, GitHub, Slack, Anthropic and OpenAI keys, private keys, `password = "..."`). Output is always redacted. | file |
| `deps`     | Specs in `requirements*.txt`, `pyproject.toml` (`[project]` and poetry) and `package.json` that don't allow the latest PyPI/npm release (`==`, `>=`/`<`, `~=`, `^`, `~`, `1.x`), with how many major versions they lag. Unbounded floors are reported once they are a major version or more behind. | manifest |
| `advisory` | Known vulnerabilities: open Dependabot alerts when the token can read them, otherwise [OSV.dev](https://osv.dev) lookups for every exact pin | manifest |
| `ci`       | Workflows whose latest run on the default branch failed, with an error excerpt from the failed job's log | workflow |
| `stale-pr` | Open PRs with no activity for `--stale-days` (default 30) | repo |

It uses only the standard library (plus `tomli` on Python 3.10), and the GitHub REST client is built on `urllib`. The Claude summarizer is optional (`pip install .[llm]`).

## Safety model

The tool is built so it can't spam repositories:

- **Dry run is the default.** `file` and `close-resolved` print what they would do. Nothing is written unless you pass `--apply`.
- **Push access is required.** Before any write, `--apply` calls `GET /repos/{owner}/{repo}` and refuses to run unless the authenticated user has `push`, `maintain` or `admin` permission.
- **Per-run cap.** A run creates at most `--max-issues` issues (default 10, hard maximum 50). Anything over the cap is listed as deferred.
- **Dedupe.** Every issue body ends with a hidden marker, `<!-- issue-autopilot fp=<16 hex> kind=<source> sig=<12 hex> -->`. The fingerprint is `sha256(kind, group)`, so it stays the same across runs.
- **Edit, don't duplicate.** `sig` is a digest of the group's signals. It leaves out line numbers, blame ages and log text. If an open issue's `sig` no longer matches, `file` rewrites its title, body and priority label in place and comments with the signals that were added or removed. Labels added by a human are kept. Edits count against `--max-issues` separately from creates.
- **Human closes win.** `file` also reads closed issues. If a human closed an autopilot issue, it is never filed again while its signals persist. `--reopen` overrides this and reopens it. `close-resolved` labels what it closes `autopilot:resolved`, and that label is how the two kinds of close are told apart. A group that comes back after an autopilot close is filed as a new issue.
- **Assignment is opt-in.** Owners from CODEOWNERS, or failing that from git blame, are only printed. `--assign --apply` assigns them and never replaces an existing assignee.
- **Nobody gets pinged.** `@mentions` in issue text are broken with a zero-width space, and PR authors are listed without `@`.
- **Resolution is conservative.** `close-resolved` only closes issues that have its own marker, and only for sources that completed successfully in the same run. For example, a PyPI outage fails the whole `deps` source, so it can't close live dependency issues.
- **Secrets are never echoed.** Matches show as `Tr0u…(22 chars)`.

## Install

```bash
pip install "issue-autopilot @ git+https://github.com/sakshamchitkara-dotcom/issue-autopilot"
# with the optional Claude summarizer:
pip install "issue-autopilot[llm] @ git+https://github.com/sakshamchitkara-dotcom/issue-autopilot"
```

The token comes from `GITHUB_TOKEN` (or `GH_TOKEN`). If neither is set, it falls back to `gh auth token`.

## Usage

```bash
autopilot scan .                       # list signals (read-only)
autopilot scan psf/requests --json     # remote repo: cloned into a temp dir
autopilot file .                       # dry run: print the issues it would file
autopilot file . --apply               # create them (needs push access; capped at 10)
autopilot file . --apply --sources todo,secret --max-issues 3
autopilot scan . --exclude 'tests/*'   # skip fixture files (repeatable glob)
autopilot file . --apply --assign      # also assign CODEOWNERS / git blame owners
autopilot file . --apply --reopen      # reopen autopilot issues a human closed, if the signals remain
autopilot close-resolved .             # dry run: list autopilot issues whose signals vanished
autopilot close-resolved . --apply     # comment on, label and close them
autopilot report . --out report.md     # read-only Markdown summary of groups and their issue status
```

`--repo owner/repo` overrides the repo that's inferred from the checkout's `origin`. `--no-llm` forces the template output.

### Claude summarizer

When `ANTHROPIC_API_KEY` is set and `anthropic` is installed, each new issue's title and intro paragraph are rewritten by `claude-opus-5-5`. The call uses structured JSON output and low effort. The signal list, labels and fingerprint marker are always generated deterministically, so the model can't break dedupe. Any API error, refusal or malformed reply falls back to the template text.

## GitHub Action

`.github/workflows/autopilot.yml` runs every Monday and can also be started by hand. It stays in dry-run mode until you set the repository variable `AUTOPILOT_APPLY=true` or tick **apply** on a manual run. It uses the built-in `GITHUB_TOKEN` with `issues: write`, and appends `autopilot report` to the job summary. That token can't read Dependabot alerts, so `advisory` falls back to OSV.dev there. To use it in another repo, copy the file there.

## Adding a source

Write `scan(ctx) -> list[Signal] | None` in `issue_autopilot/sources/<name>.py` and add it to `_registry()` in `sources/__init__.py`. Return `None` when the source doesn't apply (for example, when there's no repo). Signals that share a `(kind, group)` become one issue.

## Real run (verification log)

These runs used the throwaway repo [issue-autopilot-sandbox](https://github.com/sakshamchitkara-dotcom/issue-autopilot-sandbox). It was seeded with TODOs, a hardcoded password, old pins and a failing workflow.

Dry run (excerpt):

```
$ autopilot file ~/projects/issue-autopilot-sandbox
[dry-run] 5 issue group(s): 5 new, 0 already open, 0 over cap (10) -> sakshamchitkara-dotcom/issue-autopilot-sandbox
  1. [would create] CI failing: workflow 'check' is failing on main
     Job `check`:
     AssertionError: cache miss returned None
  2. [would create] Security: possible hardcoded secret in app/settings.py
     - [P1] `app/settings.py:2` possible generic-secret (Tr0u…(22 chars))
  3. [would create] Dependencies: 2 outdated packages in requirements.txt
  4. [would create] Tech debt: 2 FIXME/TODO comments in app/cache.py
  5. [would create] Tech debt: 1 HACK comment in app/client.py
```

Apply with a cap of 4, apply again, then a third time to show dedupe:

```
$ autopilot file ~/projects/issue-autopilot-sandbox --apply --max-issues 4
[apply] 5 issue group(s): 4 new, 0 already open, 1 over cap (4) -> sakshamchitkara-dotcom/issue-autopilot-sandbox
  ~ deferred (cap reached): todo: app/client.py
  + created #1: CI failing: workflow 'check' is failing on main
  + created #2: Security: possible hardcoded secret in app/settings.py
  + created #3: Dependencies: 2 outdated packages in requirements.txt
  + created #4: Tech debt: 2 FIXME/TODO comments in app/cache.py
created 4 issue(s) on sakshamchitkara-dotcom/issue-autopilot-sandbox as sakshamchitkara-dotcom

$ autopilot file ~/projects/issue-autopilot-sandbox --apply
[apply] 5 issue group(s): 1 new, 4 already open, 0 over cap (10) -> ...
  + created #5: Tech debt: 1 HACK comment in app/client.py

$ autopilot file ~/projects/issue-autopilot-sandbox --apply
[apply] 5 issue group(s): 0 new, 5 already open, 0 over cap (10) -> ...
created 0 issue(s) on sakshamchitkara-dotcom/issue-autopilot-sandbox as sakshamchitkara-dotcom
```

After the HACK was removed and pushed:

```
$ autopilot close-resolved ~/projects/issue-autopilot-sandbox --apply
[apply] 5 open autopilot issue(s) on sakshamchitkara-dotcom/issue-autopilot-sandbox; 1 resolved (checked sources: todo, secret, deps, ci, stale-pr)
  x closed #5: Tech debt: 1 HACK comment in app/client.py
```

Running `--apply` against a repo you can't push to is refused before anything is written:

```
$ autopilot file psf/requests --sources todo --apply
error: sakshamchitkara-dotcom has no push access to psf/requests; refusing to write issues there
```

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,llm]"
.venv/bin/pytest -q
```

All HTTP in the tests goes through a fake `urlopen` route table (`tests/conftest.py`), so the suite never touches the network.

## Known limits

- `advisory` via OSV.dev only checks exact pins. A range such as `>=2,<3` has no single version to look up. Dependabot alerts cover ranges and lockfiles.
- `deps` skips npm `||` and hyphen ranges instead of guessing, and it doesn't read lockfiles.
- CODEOWNERS matching uses `fnmatch`, where `*` also crosses `/`. Team owners (`@org/team`) can't be assigned, so they are skipped.
- The resolved/human close split depends on the `autopilot:resolved` label. Issues closed by v0.1 don't have it, so they count as closed by a human. If a human reopens and then closes a labelled issue, it still counts as an autopilot close.
