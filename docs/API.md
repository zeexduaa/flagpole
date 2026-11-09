# flagpole HTTP API (v0.1)

All requests and responses are JSON. Send bodies with `Content-Type: application/json` (max 256 KB). Every `GET` endpoint also answers `HEAD` (same status and headers, no body).

## Authentication

```
Authorization: Bearer <token>
```

| Token  | Can                                                                      |
|--------|--------------------------------------------------------------------------|
| admin  | everything                                                               |
| client | `GET /api/snapshot` and `POST /api/evaluate` only                        |

A missing or wrong token gets `401`; a client token on an admin endpoint gets `403`.

Admin requests may add `X-Flagpole-Actor: <name>` (up to 64 letters, digits or `. _ @ + -`) to record who made a change in the audit log. Without it the actor is `admin`.

## Errors

Every error has the same shape:

```json
{"error": {"code": "invalid_flag", "message": "percentages must add up to 100, not 90", "path": "default.rollout"}}
```

| Status | `code`                                                      | When                                              |
|--------|-------------------------------------------------------------|---------------------------------------------------|
| 400    | `invalid_json`, `invalid_if_match`, `invalid_query`, `invalid_actor`, `bad_request` | malformed request              |
| 401    | `unauthorized`                                              | missing or wrong token                            |
| 403    | `forbidden`                                                 | client token on an admin endpoint                 |
| 404    | `not_found`, `flag_not_found`                               | unknown path or flag                              |
| 405    | `method_not_allowed`                                        | wrong method (see the `Allow` header)             |
| 409    | `flag_exists`                                               | creating a key that exists                        |
| 412    | `version_conflict`                                          | `If-Match` doesn't match; `ETag` has the current version |
| 413    | `body_too_large`                                            | body over 256 KB                                  |
| 415    | `unsupported_media_type`                                    | body isn't `application/json`                     |
| 422    | `invalid_flag`, `invalid_request`                           | validation failed; `path` names the field         |
| 500    | `internal_error`                                            | a bug; details are only in the server log         |

## Flags

### The flag object

```json
{
  "key": "new-checkout",
  "description": "Redesigned checkout flow",
  "enabled": true,
  "variants": {"on": true, "off": false},
  "off_variant": "off",
  "default": {"rollout": {"on": 10, "off": 90}},
  "rules": [
    {
      "description": "Staff always get it",
      "conditions": [{"attribute": "email", "op": "ends_with", "value": "@example.com"}],
      "serve": {"variant": "on"}
    }
  ],
  "bucket_by": "key",
  "tags": ["checkout"],
  "version": 3,
  "created_at": "2026-11-09T11:57:41.367Z",
  "updated_at": "2026-11-09T12:10:02.004Z"
}
```

| Field         | Rules                                                                                     | Default            |
|---------------|-------------------------------------------------------------------------------------------|--------------------|
| `key`         | 1–64 of `a-z 0-9 . _ -`, starting with a letter or digit. Can't be changed.               | required           |
| `description` | up to 500 characters                                                                       | `""`               |
| `enabled`     | boolean                                                                                    | `false`            |
| `variants`    | 2–20 named values (any JSON); names follow the key rules                                   | `{"on": true, "off": false}` |
| `off_variant` | a variant name; served when disabled or when a rollout can't bucket                        | `off`, else the first variant |
| `default`     | a serve (see below)                                                                        | `on`, else the first non-off variant |
| `rules`       | up to 100, checked in order                                                                | `[]`               |
| `bucket_by`   | context attribute used for rollouts (dotted paths allowed)                                 | `key`              |
| `tags`        | short lowercase labels                                                                     | `[]`               |
| `version`, `created_at`, `updated_at` | set by the server; ignored in request bodies                       |                    |

A **serve** is either `{"variant": "on"}` or `{"rollout": {"on": 25, "off": 75}}`. Rollout percentages must add up to 100, with at most 2 decimals.

A **rule** has 1–20 `conditions` (all must match), a `serve` and an optional `description`.

A **condition** is `{"attribute": "country", "op": "in", "value": ["DE", "AT"]}`:

| `op`                                         | `value`                       |
|----------------------------------------------|-------------------------------|
| `eq`, `neq`                                  | string, number, boolean, null |
| `in`, `not_in`                               | non-empty list of those       |
| `contains`, `not_contains`, `starts_with`, `ends_with` | non-empty string    |
| `matches`                                    | regular expression (≤ 200 chars), searched anywhere |
| `gt`, `gte`, `lt`, `lte`                     | number (only numbers compare) |
| `exists`, `not_exists`                       | no `value`                    |

### `GET /api/flags[?tag=TAG]` (admin)

`200 {"revision": 12, "flags": [<flag>, ...]}`, sorted by key.

### `POST /api/flags` (admin)

Body: a flag (only `key` is required). `201` with the saved flag, `Location: /api/flags/{key}` and `ETag: "1"`.

### `GET /api/flags/{key}` (admin)

`200 <flag>` with `ETag: "<version>"`.

### `PUT /api/flags/{key}` (admin)

Replaces all editable fields; omitted fields get their defaults. Optional `If-Match: "<version>"`. Returns `200 <flag>` with the new `ETag`. Saving an identical definition is a no-op (same version, no audit entry).

### `POST /api/flags/{key}/enable` and `/disable` (admin)

Optional `If-Match`. `200 <flag>`.

### `DELETE /api/flags/{key}` (admin)

Optional `If-Match`. `204`. Apps that still check the flag get their default.

## Audit log

### `GET /api/audit?flag=KEY&limit=50&before=ID` (admin)

Newest first; `limit` is 1–500 (anything else is a `400`). Pass `before` = `next_before` from the previous page to continue.

```json
{
  "entries": [
    {"id": 7, "at": "2026-11-09T12:10:02.004Z", "actor": "sam@example.com", "action": "enable",
     "flag": "new-checkout", "version": 3, "before": {...}, "after": {...}}
  ],
  "next_before": 7
}
```

`action` is `create`, `update`, `enable`, `disable` or `delete`. `before` is null for creates; `after` is null for deletes.

## For apps

### `GET /api/snapshot` (client)

```json
{"revision": 12, "flags": [<flag>, ...]}
```

Responds with `ETag: "r<revision>"`. Send it back as `If-None-Match` and get `304 Not Modified` (empty body) until something changes. Weak tags (`W/"r12"`), comma-separated lists and `*` are understood too. This is what the Python SDK polls.

### `POST /api/evaluate` (client)

```json
{"context": {"key": "user-2", "country": "DE", "plan": "pro"}, "flags": ["new-checkout", "dark-mode"]}
```

`flags` is optional (all flags if omitted).

```json
{
  "results": {
    "new-checkout": {"flag": "new-checkout", "variant": "on", "value": true,
                     "reason": "rule_match_rollout", "rule": 1, "version": 3},
    "dark-mode": {"flag": "dark-mode", "variant": null, "value": null, "reason": "flag_not_found"}
  }
}
```

`reason` is one of `disabled`, `rule_match`, `rule_match_rollout`, `default`, `default_rollout`, `missing_bucket_attribute`, `flag_not_found`. `rule` is the index of the matching rule.

## Health

### `GET /health` (no auth)

`200 {"status": "ok", "version": "0.1.0", "revision": 12}`
