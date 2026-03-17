# issue-autopilot

Turns signals already sitting in a repository into deduplicated GitHub issues, and closes those issues again once the signals are gone.

| Source     | What it finds | Grouped into one issue per |
|------------|---------------|----------------------------|
| `todo`     | `TODO` / `FIXME` / `HACK` / `XXX` comments, with the `git blame` author and age | file |
| `secret`   | Hardcoded credentials (AWS, GitHub, Slack, Anthropic and OpenAI keys, private keys, `password = "..."`). Output is always redacted. | file |
| `deps`     | `==` pins in `requirements*.txt` and versions in `package.json` that are behind the latest release on PyPI or npm | manifest |
| `ci`       | Workflows whose latest run on the default branch failed, with an error excerpt from the failed job's log | workflow |
| `stale-pr` | Open PRs with no activity for `--stale-days` (default 30) | repo |

It has no runtime dependencies: it uses only the standard library, and the GitHub REST client is built on `urllib`. The Claude summarizer is optional (`pip install .[llm]`).

## Safety model

The tool is built so it can't spam repositories:

- **Dry run is the default.** `file` and `close-resolved` print what they would do. Nothing is written unless you pass `--apply`.
- **Push access is required.** Before any write, `--apply` calls `GET /repos/{owner}/{repo}` and refuses to run unless the authenticated user has `push`, `maintain` or `admin` permission.
- **Per-run cap.** A run creates at most `--max-issues` issues (default 10, hard maximum 50). Anything over the cap is listed as deferred.
- **Dedupe.** Every issue body ends with a hidden marker, `<!-- issue-autopilot fp=<16 hex> kind=<source> -->`. The fingerprint is `sha256(kind, group)`, so it stays the same across runs. Open issues that carry a matching marker are skipped.
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
autopilot close-resolved .             # dry run: list autopilot issues whose signals vanished
autopilot close-resolved . --apply     # comment on and close them
```

`--repo owner/repo` overrides the repo that's inferred from the checkout's `origin`. `--no-llm` forces the template output.

### Claude summarizer

When `ANTHROPIC_API_KEY` is set and `anthropic` is installed, each new issue's title and intro paragraph are rewritten by `claude-opus-5-5`. The call uses structured JSON output and low effort. The signal list, labels and fingerprint marker are always generated deterministically, so the model can't break dedupe. Any API error, refusal or malformed reply falls back to the template text.

## GitHub Action

`.github/workflows/autopilot.yml` runs every Monday and can also be started by hand. It stays in dry-run mode until you set the repository variable `AUTOPILOT_APPLY=true` or tick **apply** on a manual run. It uses the built-in `GITHUB_TOKEN` with `issues: write`. To use it in another repo, copy the file there.

## Adding a source

Write `scan(ctx) -> list[Signal] | None` in `issue_autopilot/sources/<name>.py` and add it to the registry in `sources/__init__.py`. Return `None` when the source doesn't apply (for example, when there's no repo). Signals that share a `(kind, group)` become one issue.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,llm]"
.venv/bin/pytest -q
```

All HTTP in the tests goes through a fake `urlopen` route table (`tests/conftest.py`), so the suite never touches the network.

## Known limits

- An existing issue's body isn't updated when its group changes (for example, a new TODO in a file that already has an open issue). The issue stays open until the whole group disappears.
- If you close an autopilot issue by hand, the next run will re-file it while the signal still exists. Delete the code comment or pin, or don't use `--apply` for that source.
- The dependency check only reads exact pins. Ranges like `>=` are skipped.
