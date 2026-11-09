# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- Rollout percentages such as `0.29` or `1.15` were rejected because of floating-point rounding (#29).
- `HEAD` requests returned `501`; they now behave like `GET` without a body (#30).
- `GET /api/snapshot` now honours weak tags (`W/"r3"`), lists and `*` in `If-None-Match` (#31).
- `GET /api/audit` rejects a `limit` outside 1–500 instead of silently changing it (#32).
- The CLI's `--db` option now also works after the subcommand, e.g. `flagpole flags list --db x.db` (#33).
- Server startup no longer does a slow reverse DNS lookup, and oversized requests get a `413` instead of a connection reset.

### Added

- Flag model with variants, ordered targeting rules (15 operators, nested attributes) and stable percentage rollouts.
- Validation errors that name the exact field (e.g. `rules[0].conditions[1].op`).
- SQLite store with per-flag versions, optimistic concurrency, an audit log, a revision counter and schema migrations.
- REST API with admin and client tokens, ETag/If-Match, `304 Not Modified` snapshots and consistent JSON errors.
- Python SDK (`FlagpoleClient`) with local evaluation, background refresh and safe fallbacks.
- CLI: `serve`, `flags` (list/show/create/update/enable/disable/delete), `eval`, `audit`, `export`.
- Tests on Linux, macOS and Windows for Python 3.11 and 3.13.
