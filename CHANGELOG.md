# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Flag model with variants, ordered targeting rules (15 operators, nested attributes) and stable percentage rollouts.
- Validation errors that name the exact field (e.g. `rules[0].conditions[1].op`).
- SQLite store with per-flag versions, optimistic concurrency, an audit log, a revision counter and schema migrations.
- REST API with admin and client tokens, ETag/If-Match, `304 Not Modified` snapshots and consistent JSON errors.
- Python SDK (`FlagpoleClient`) with local evaluation, background refresh and safe fallbacks.
- CLI: `serve`, `flags` (list/show/create/update/enable/disable/delete), `eval`, `audit`, `export`.
- Tests on Linux, macOS and Windows for Python 3.11 and 3.13.
