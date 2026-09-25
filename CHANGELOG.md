# Changelog

## 0.4.0 - 2026-09-25

### Added
- **`yarn.lock` and `pnpm-lock.yaml` in `advisory`.** The OSV.dev fallback now reads yarn classic and berry lockfiles and pnpm v5, v6 and v9 lockfiles, so range specs in `package.json` are checked for yarn and pnpm projects too.
- **More of `pyproject.toml` in `deps` and `advisory`:** `[project.optional-dependencies]` and PEP 735 `[dependency-groups]`.

### Fixed
- Pre-release and post-release suffixes were read as an extra version number (`1.0rc1` as 1.0.1). That could name a fix older than the installed version as the upgrade target. Only the release numbers are compared now.

## 0.3.0 - 2026-09-25

### Added
- **`autopilot doctor`.** It runs read-only checks on git, the checkout, the repo, the token and its identity, push access, whether issues, Dependabot alerts, Actions runs and PRs are readable, CODEOWNERS team owners, and the summarizer. Exits 1 on failures. The scheduled workflow runs it first.
- **`actions` source.** Workflow `uses: owner/repo@vN` steps that are one or more major versions behind the action's latest release (P2 when two or more). SHA and branch pins are skipped.
- **`flaky` source.** Jobs that failed and then passed on the same commit, from re-runs or from disagreeing runs, at least twice in the last 500 completed runs.
- **`--json` on `file`, `close-resolved` and `report`**, joining `scan` and `doctor`.
- **Lockfiles in `advisory`.** The OSV.dev fallback now checks every package in `package-lock.json`, `poetry.lock` and `uv.lock`, so range specs are covered.
- **npm `||` and hyphen ranges** in `deps`.

### Fixed
- `--apply` from GitHub Actions: the built-in `GITHUB_TOKEN` (and any GitHub App installation token) was refused, because it can't read `/user`. Write access for installation tokens is now checked against `/installation/repositories`.
- Back-to-back `file --apply` runs could file a duplicate while the issue listing lagged behind. Issue numbers past the listing are now fetched directly.
- CODEOWNERS globs follow GitHub's rules: `*` doesn't cross `/`, `**` does, a middle slash anchors the pattern, and `dir/*` covers only direct children.
- An `autopilot:resolved` issue that a human reopened and closed again is now treated as closed by a human, based on its events.
- Unknown `--sources` fail before cloning or scanning, spaces in the list are accepted, and `close-resolved --max-issues` is bounded to 1..50.

### Changed
- The workflows use `actions/checkout@v7` and `actions/setup-python@v7`.
- The User-Agent now carries the package version.

## 0.2.0 - 2026-09-25

### Added
- **Edit in place.** Every issue marker now carries a signal digest (`sig=`). When a group's signals change, `file` rewrites the open issue's title, body and priority label and posts a changelog comment that lists the added and removed signals. Labels added by a human are kept. Pure line-number moves, blame ages and new CI run links don't count as changes. A change that only touches formatting updates the body without a comment.
- **Manual closes are respected.** `file` looks at closed autopilot issues too. If a human closed an issue whose signals still exist, `file` doesn't file it again. `--reopen` reopens such issues instead. `close-resolved` labels the issues it closes `autopilot:resolved`, and a group that comes back after one of those closes is filed as a new issue.
- **Version ranges in `deps`.** It now understands `>=`, `>`, `<`, `<=`, `~=`, wildcards, and npm/poetry `^`, `~` and `1.x`. A dependency is flagged when its spec doesn't allow the latest release.
- **pyproject.toml** support in `deps`: `[project].dependencies` and poetry dependency tables, including groups.
- **Major-version lag.** Summaries now say `(2 majors behind)`. Unbounded floors are flagged at P3 once they are at least one major behind.
- **`advisory` source.** It reads open Dependabot alerts through the API. When those are disabled or not readable, it queries OSV.dev for exact pins. Aliases are collapsed, and high/critical advisories are P1.
- **Owner suggestions.** Owners come from CODEOWNERS, falling back to git blame mapped to a GitHub login. They are printed by default and assigned only with `file --assign --apply`. An existing assignee is never replaced.
- **`autopilot report`:** a read-only Markdown summary (priority counts, per-group issue status, resolved issues, warnings). The scheduled workflow appends it to the job summary.

### Changed
- For `^`/`~` specs, `deps` now reports only releases outside the allowed range. For example, `^17.0.2` against `17.5.0` is no longer flagged.
- On Python 3.10, `tomli` is now a dependency, used to read `pyproject.toml`.

### Upgrading
- Issues filed by 0.1 have no digest. The first 0.2 run updates each of them once, without a comment unless the signals really changed.
- Issues closed by 0.1's `close-resolved` have no `autopilot:resolved` label, so 0.2 treats them as closed by a human.

## 0.1.0

The first release. It has the `todo`, `secret`, `deps`, `ci` and `stale-pr` sources, fingerprint dedupe, the `scan`, `file` and `close-resolved` commands with dry-run as the default, the push-access guard, the per-run cap, and the optional Claude summarizer.
