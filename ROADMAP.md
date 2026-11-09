# Roadmap

flagpole aims to be the feature-flag service you can run in five minutes and trust in production.
v0.1 has the core (engine, store, API, Python SDK, CLI). This is what's next.

Sizes are rough guesses (S = a few hours, M = about a day, L = several days, XL = a week or more).

## ✅ v0.1: the core

- Variants, targeting rules with 15 operators, stable percentage rollouts
- SQLite store with versions, optimistic concurrency, audit log and migrations
- REST API with admin/client tokens, ETags and consistent errors
- Python SDK with local evaluation and background refresh
- CLI; tests on Linux, macOS and Windows

## v0.2: environments and projects

- [ ] **Environments** (development, staging, production) with separate flag states and tokens per environment (XL)
- [ ] **Projects** to group flags for different apps (L)
- [ ] **Promote a flag** from one environment to another with a diff preview (M)
- [x] **Import a snapshot** (`flagpole import`) to restore or seed a database (M)

## v0.3: smarter targeting

- [ ] **Segments**: named, reusable groups of conditions ("beta testers", "EU customers") referenced from rules (L)
- [ ] **Semantic version operators** (`semver_gt`, `semver_lt`, ...) for app-version targeting (M)
- [ ] **Date and time operators** for scheduled launches (`before`, `after`) (M)
- [ ] **Scheduled changes**: enable a flag or change a rollout at a set time (L)
- [ ] **Flag prerequisites**: only evaluate a flag if another one is on (M)
- [ ] **Faster large `in` lists** by precomputing hash sets per condition (S)

## v0.4: real time and scale

- [ ] **Server-sent events** stream so SDKs get changes within a second instead of polling (L)
- [ ] **PostgreSQL store** behind the same interface, for multi-instance deployments (XL)
- [ ] **Evaluation metrics**: count how often each variant is served, exposed at `/metrics` for Prometheus (L)
- [ ] **Stale flag report**: flags that haven't changed or been evaluated in N days (M)
- [ ] **Rate limiting** per token (M)

## v0.5: people and tools

- [ ] **Web dashboard** to view, edit and toggle flags and read the audit log (XL)
- [ ] **Per-person tokens and roles** (viewer, editor, admin) instead of one shared admin token (L)
- [ ] **Webhooks** on flag changes, e.g. to post in Slack (M)
- [ ] **Revert to a previous version** from the audit log (M)
- [ ] **OpenAPI document** served at `/openapi.json`, generated from the routes (M)

## v0.6: more SDKs

- [ ] **JavaScript/TypeScript SDK** for browsers and Node, sharing the evaluation spec (XL)
- [ ] **Shared evaluation test suite** in JSON so every SDK is checked against the same cases (M)
- [ ] **Go SDK** (L)

## v1.0: production ready

- [ ] **Docker image** and a docker-compose example with Caddy for HTTPS (M)
- [x] **Backups**: `flagpole backup` using SQLite's online backup API (S)
- [ ] **Load test** and published numbers for evaluate/snapshot throughput (M)
- [ ] **Publish to PyPI** (S)
- [ ] **Docs site** with guides: gradual rollouts, kill switches, A/B tests (L)
