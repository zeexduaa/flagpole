"""Command-line interface.

    flagpole serve [--dev]                  run the API server
    flagpole flags list | show | create | update | enable | disable | delete
    flagpole eval KEY --context '{"key": "user-1"}'
    flagpole audit [--flag KEY]
    flagpole export > snapshot.json

Management commands work directly on the database file (``--db`` or $FLAGPOLE_DB),
so they need no token: whoever can read the file can manage it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .flags import FlagError
from .server import Tokens, create_server
from .store import FlagStore, StoreError

ADMIN_ENV = "FLAGPOLE_ADMIN_TOKEN"
CLIENT_ENV = "FLAGPOLE_CLIENT_TOKEN"
DB_ENV = "FLAGPOLE_DB"


def load_dotenv(path: str = ".env", env: dict[str, str] | None = None) -> int:
    """Reads KEY=value lines from a .env file; variables already set win. A missing file is fine."""
    target = os.environ if env is None else env
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    added = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and value and key not in target:
            target[key] = value
            added += 1
    return added


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _read_json(source: str) -> Any:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"flagpole: {source} isn't valid JSON: {exc}") from None


def _summary(flag: Any) -> str:
    state = "ON " if flag.enabled else "off"
    rules = f"{len(flag.rules)} rule(s)" if flag.rules else "no rules"
    tags = f"  [{', '.join(flag.tags)}]" if flag.tags else ""
    return f"{state}  {flag.key:<32} v{flag.version:<4} {rules}{tags}"


# ---- Commands ------------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    admin = os.environ.get(ADMIN_ENV, "")
    client = os.environ.get(CLIENT_ENV, "")
    if args.dev:
        if not admin:
            admin = secrets.token_urlsafe(24)
            print(f"dev admin token:  {admin}")
        if not client:
            client = secrets.token_urlsafe(24)
            print(f"dev client token: {client}")
    try:
        tokens = Tokens(admin, client)
    except ValueError as exc:
        print(f"flagpole: {exc}. Set {ADMIN_ENV} and {CLIENT_ENV} (see .env.example), "
              "or use --dev for throwaway tokens.", file=sys.stderr)
        return 2
    store = FlagStore(args.db)
    server = create_server(store, tokens, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"flagpole {__version__} serving {args.db} on http://{host}:{port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        store.close()
    return 0


def cmd_list(args: argparse.Namespace, store: FlagStore) -> int:
    flags = store.list(tag=args.tag)
    if args.json:
        _print_json([f.to_dict() for f in flags])
    elif not flags:
        print("No flags yet. Create one with: flagpole flags create my-flag")
    else:
        for flag in flags:
            print(_summary(flag))
    return 0


def cmd_show(args: argparse.Namespace, store: FlagStore) -> int:
    _print_json(store.require(args.key).to_dict())
    return 0


def cmd_create(args: argparse.Namespace, store: FlagStore) -> int:
    data: dict[str, Any] = _read_json(args.json) if args.json else {}
    if not isinstance(data, dict):
        raise SystemExit("flagpole: the JSON must be an object")
    data["key"] = args.key
    if args.description is not None:
        data["description"] = args.description
    flag = store.create(data, actor=args.actor)
    print(f"Created {flag.key} (disabled). Turn it on with: flagpole flags enable {flag.key}"
          if not flag.enabled else f"Created {flag.key} (enabled).")
    return 0


def cmd_update(args: argparse.Namespace, store: FlagStore) -> int:
    data = _read_json(args.json)
    if not isinstance(data, dict):
        raise SystemExit("flagpole: the JSON must be an object")
    flag = store.update(args.key, data, actor=args.actor, expected_version=args.if_version)
    print(f"Saved {flag.key} as version {flag.version}.")
    return 0


def cmd_enable(args: argparse.Namespace, store: FlagStore, enabled: bool) -> int:
    flag = store.set_enabled(args.key, enabled, actor=args.actor)
    print(f"{flag.key} is now {'ON' if flag.enabled else 'off'} (version {flag.version}).")
    return 0


def cmd_delete(args: argparse.Namespace, store: FlagStore) -> int:
    store.require(args.key)
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("flagpole: add --yes to delete without a prompt")
        if input(f"Delete {args.key}? Apps will get their default value. [y/N] ").strip().lower() != "y":
            print("Not deleted.")
            return 1
    store.delete(args.key, actor=args.actor)
    print(f"Deleted {args.key}.")
    return 0


def cmd_eval(args: argparse.Namespace, store: FlagStore) -> int:
    try:
        context = json.loads(args.context)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"flagpole: --context isn't valid JSON: {exc}") from None
    if not isinstance(context, dict):
        raise SystemExit("flagpole: --context must be a JSON object")
    _print_json(store.require(args.key).evaluate(context).to_dict())
    return 0


def cmd_audit(args: argparse.Namespace, store: FlagStore) -> int:
    entries = store.audit(flag=args.flag, limit=args.limit)
    if args.json:
        _print_json([e.to_dict() for e in entries])
        return 0
    if not entries:
        print("No changes recorded yet.")
    for e in entries:
        print(f"{e.at}  {e.actor:<16} {e.action:<8} {e.flag} (v{e.version})")
    return 0


def cmd_export(args: argparse.Namespace, store: FlagStore) -> int:
    revision, flags = store.snapshot()
    _print_json({"revision": revision, "flags": [f.to_dict() for f in flags]})
    return 0


# ---- Parser --------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flagpole", description="A small self-hosted feature-flag service.")
    parser.add_argument("--version", action="version", version=f"flagpole {__version__}")
    parser.add_argument("--db", default=os.environ.get(DB_ENV, "flagpole.db"),
                        help=f"SQLite database file (default: ${DB_ENV} or flagpole.db)")
    # Also accept --db after the subcommand ("flagpole flags list --db x.db"). SUPPRESS means
    # the subcommand only overrides the top-level value when --db is actually given there.
    db = argparse.ArgumentParser(add_help=False)
    db.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database file")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the API server", parents=[db])
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--dev", action="store_true", help="make up throwaway tokens if none are set")
    p.set_defaults(func=cmd_serve, needs_store=False)

    actor = argparse.ArgumentParser(add_help=False, parents=[db])
    actor.add_argument("--actor", default=_default_actor(), help="name recorded in the audit log")

    flags = sub.add_parser("flags", help="manage flags").add_subparsers(dest="flags_command", required=True)
    p = flags.add_parser("list", help="list flags", parents=[db])
    p.add_argument("--tag")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)
    p = flags.add_parser("show", help="print a flag as JSON", parents=[db])
    p.add_argument("key")
    p.set_defaults(func=cmd_show)
    p = flags.add_parser("create", help="create a flag (disabled, on/off variants unless --json says otherwise)",
                         parents=[actor])
    p.add_argument("key")
    p.add_argument("--description")
    p.add_argument("--json", metavar="FILE", help="flag definition as JSON (- for stdin)")
    p.set_defaults(func=cmd_create)
    p = flags.add_parser("update", help="replace a flag's definition from JSON", parents=[actor])
    p.add_argument("key")
    p.add_argument("--json", metavar="FILE", required=True, help="flag definition as JSON (- for stdin)")
    p.add_argument("--if-version", type=int, help="only save if the flag is still at this version")
    p.set_defaults(func=cmd_update)
    for name, enabled in (("enable", True), ("disable", False)):
        p = flags.add_parser(name, help=f"{name} a flag", parents=[actor])
        p.add_argument("key")
        p.set_defaults(func=lambda a, s, e=enabled: cmd_enable(a, s, e))
    p = flags.add_parser("delete", help="delete a flag", parents=[actor])
    p.add_argument("key")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("eval", help="evaluate a flag for a context", parents=[db])
    p.add_argument("key")
    p.add_argument("--context", default="{}", help='JSON, e.g. \'{"key": "user-1", "country": "DE"}\'')
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("audit", help="show recent changes", parents=[db])
    p.add_argument("--flag")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("export", help="print all flags as a snapshot (JSON)", parents=[db])
    p.set_defaults(func=cmd_export)
    return parser


def _default_actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:
        return "cli"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        if getattr(args, "needs_store", True):
            with FlagStore(args.db) as store:
                return args.func(args, store)
        return args.func(args)
    except FlagError as exc:
        print(f"flagpole: invalid flag: {exc}", file=sys.stderr)
        return 2
    except StoreError as exc:
        print(f"flagpole: {exc}", file=sys.stderr)
        return 1
