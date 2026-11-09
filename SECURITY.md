# Security

## Reporting a problem

Please don't open a public issue for a security problem. Use GitHub's private
[security advisory form](https://github.com/zeexduaa/flagpole/security/advisories/new) instead.

## Running flagpole safely

- **Use long random tokens.** `python -c "import secrets; print(secrets.token_urlsafe(32))"`. The server refuses tokens shorter than 16 characters, or an admin token equal to the client token.
- **Keep the admin token out of apps.** Apps only need the client token, which can read and evaluate flags but not change them.
- **Treat flags as visible to clients.** Anyone with the client token can download every flag definition, including rule values such as email domains or account IDs. Don't put secrets in flags.
- **Use HTTPS.** flagpole speaks plain HTTP and listens on `127.0.0.1` by default. Put a reverse proxy with TLS (Caddy, nginx) in front before exposing it to a network.
- **Protect the database file.** Anyone who can write `flagpole.db` can change flags without a token (that's how the CLI works).

## What flagpole does

- Compares tokens in constant time and never writes them to its logs.
- Limits request bodies to 256 KB and regular expressions in rules to 200 characters.
- Returns a generic message for internal errors; details go only to the server log.
- Sends `X-Content-Type-Options: nosniff` and `Cache-Control: no-store` on API responses.

## Known limitations (v0.1)

- One shared admin token: there are no per-person accounts yet (see the roadmap).
- No rate limiting.
- Regular expressions use Python's `re`, which can be slow on pathological patterns. Only admins can create rules, but review `matches` conditions with care.
