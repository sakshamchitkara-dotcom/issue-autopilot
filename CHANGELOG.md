# Changelog

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
