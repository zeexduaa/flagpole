"""SQLite storage for flags, with optimistic concurrency and an audit log.

* Every flag has a ``version`` that goes up by one on each change. Writers can pass
  the version they last saw; if someone else changed the flag in between, the write
  is refused with :class:`VersionConflict` instead of silently overwriting it.
* Every change is recorded in ``audit_log`` with who made it and the flag before
  and after, in the same transaction as the change itself.
* A store-wide ``revision`` goes up on every change, so clients can cheaply ask
  "has anything changed since revision N?" (the API uses it as an ETag).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .flags import Flag, FlagError

#: Each entry upgrades the schema by one version. Never edit a released entry: add a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE flags (
        key         TEXT PRIMARY KEY,
        definition  TEXT NOT NULL,
        version     INTEGER NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE audit_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        at          TEXT NOT NULL,
        actor       TEXT NOT NULL,
        action      TEXT NOT NULL,
        flag_key    TEXT NOT NULL,
        version     INTEGER NOT NULL,
        before      TEXT,
        after       TEXT
    );
    CREATE INDEX audit_log_by_flag ON audit_log (flag_key, id);
    CREATE TABLE meta (
        name        TEXT PRIMARY KEY,
        value       INTEGER NOT NULL
    );
    INSERT INTO meta (name, value) VALUES ('revision', 0);
    """,
]


class StoreError(Exception):
    pass


class NotFound(StoreError):
    def __init__(self, key: str):
        super().__init__(f"flag {key!r} not found")
        self.key = key


class AlreadyExists(StoreError):
    def __init__(self, key: str):
        super().__init__(f"flag {key!r} already exists")
        self.key = key


class VersionConflict(StoreError):
    def __init__(self, key: str, expected: int, current: int):
        super().__init__(
            f"flag {key!r} was changed by someone else (you had version {expected}, it is now {current}); "
            "reload it and try again"
        )
        self.key, self.expected, self.current = key, expected, current


@dataclass(frozen=True)
class AuditEntry:
    id: int
    at: str
    actor: str
    action: str
    flag: str
    version: int
    before: dict[str, Any] | None
    after: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "at": self.at, "actor": self.actor, "action": self.action,
            "flag": self.flag, "version": self.version, "before": self.before, "after": self.after,
        }


@dataclass
class ImportResult:
    created: list[str]
    updated: list[str]
    unchanged: list[str]
    #: Flags that already existed and were left alone (import without overwrite).
    skipped: list[str]

    def summary(self) -> str:
        return (f"{len(self.created)} created, {len(self.updated)} updated, "
                f"{len(self.unchanged)} unchanged, {len(self.skipped)} skipped")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FlagStore:
    """Thread-safe access to one SQLite database file (or ``":memory:"``)."""

    def __init__(self, path: str | Path = "flagpole.db", now: Callable[[], str] = utc_now):
        self.path = str(path)
        self._now = now
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # ---- Housekeeping --------------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            current = self._db.execute("PRAGMA user_version").fetchone()[0]
            if current > len(MIGRATIONS):
                raise StoreError(
                    f"{self.path} was created by a newer flagpole (schema {current}); upgrade flagpole to open it"
                )
            for version in range(current, len(MIGRATIONS)):
                self._db.executescript(f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version + 1};\nCOMMIT;")

    def schema_version(self) -> int:
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> FlagStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; takes the write lock up front so versions can't race."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    # ---- Reading -------------------------------------------------------------------------

    def _row_to_flag(self, row: sqlite3.Row) -> Flag:
        data = json.loads(row["definition"])
        data.update(version=row["version"], created_at=row["created_at"], updated_at=row["updated_at"])
        return Flag.from_dict(data, key=row["key"])

    def get(self, key: str) -> Flag | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM flags WHERE key = ?", (key,)).fetchone()
        return self._row_to_flag(row) if row else None

    def require(self, key: str) -> Flag:
        flag = self.get(key)
        if flag is None:
            raise NotFound(key)
        return flag

    def list(self, tag: str | None = None) -> list[Flag]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM flags ORDER BY key").fetchall()
        flags = [self._row_to_flag(r) for r in rows]
        return [f for f in flags if tag in f.tags] if tag else flags

    def revision(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT value FROM meta WHERE name = 'revision'").fetchone()[0])

    def snapshot(self) -> tuple[int, list[Flag]]:
        """All flags and the revision they belong to, read consistently."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                revision = int(self._db.execute("SELECT value FROM meta WHERE name = 'revision'").fetchone()[0])
                rows = self._db.execute("SELECT * FROM flags ORDER BY key").fetchall()
            finally:
                self._db.execute("COMMIT")
        return revision, [self._row_to_flag(r) for r in rows]

    def audit(self, flag: str | None = None, limit: int = 50, before_id: int | None = None) -> list[AuditEntry]:
        """Newest first. Page through with ``before_id`` = the last id you received."""
        limit = max(1, min(limit, 500))
        sql = "SELECT * FROM audit_log WHERE 1 = 1"
        args: list[Any] = []
        if flag is not None:
            sql += " AND flag_key = ?"
            args.append(flag)
        if before_id is not None:
            sql += " AND id < ?"
            args.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [
            AuditEntry(
                r["id"], r["at"], r["actor"], r["action"], r["flag_key"], r["version"],
                json.loads(r["before"]) if r["before"] else None,
                json.loads(r["after"]) if r["after"] else None,
            )
            for r in rows
        ]

    # ---- Writing -------------------------------------------------------------------------

    def _record(self, db: sqlite3.Connection, actor: str, action: str, key: str, version: int,
                before: Flag | None, after: Flag | None, at: str) -> None:
        db.execute(
            "INSERT INTO audit_log (at, actor, action, flag_key, version, before, after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (at, actor, action, key, version,
             json.dumps(before.definition()) if before else None,
             json.dumps(after.definition()) if after else None),
        )
        db.execute("UPDATE meta SET value = value + 1 WHERE name = 'revision'")

    def create(self, data: dict[str, Any], actor: str) -> Flag:
        """Validates and saves a new flag. Raises FlagError or AlreadyExists."""
        flag = Flag.from_dict(data)
        at = self._now()
        with self._write() as db:
            if db.execute("SELECT 1 FROM flags WHERE key = ?", (flag.key,)).fetchone():
                raise AlreadyExists(flag.key)
            db.execute(
                "INSERT INTO flags (key, definition, version, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                (flag.key, json.dumps(flag.definition()), at, at),
            )
            saved = Flag.from_dict({**flag.definition(), "version": 1, "created_at": at, "updated_at": at},
                                   key=flag.key)
            self._record(db, actor, "create", flag.key, 1, None, saved, at)
        return saved

    def _change(self, key: str, actor: str, action: str, expected_version: int | None,
                make: Callable[[Flag], dict[str, Any]]) -> Flag:
        at = self._now()
        with self._write() as db:
            row = db.execute("SELECT * FROM flags WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise NotFound(key)
            before = self._row_to_flag(row)
            if expected_version is not None and expected_version != before.version:
                raise VersionConflict(key, expected_version, before.version)
            definition = make(before)
            candidate = Flag.from_dict(definition, key=key)  # validates
            if candidate.definition() == before.definition():
                return before  # nothing changed: no new version, no audit noise
            version = before.version + 1
            db.execute(
                "UPDATE flags SET definition = ?, version = ?, updated_at = ? WHERE key = ?",
                (json.dumps(candidate.definition()), version, at, key),
            )
            after = Flag.from_dict(
                {**candidate.definition(), "version": version, "created_at": before.created_at, "updated_at": at},
                key=key,
            )
            self._record(db, actor, action, key, version, before, after, at)
        return after

    def update(self, key: str, data: dict[str, Any], actor: str, expected_version: int | None = None) -> Flag:
        """Replaces the flag's definition (all editable fields; omitted ones get their defaults)."""
        body = {k: v for k, v in data.items() if k not in ("version", "created_at", "updated_at")}
        return self._change(key, actor, "update", expected_version, lambda _: body)

    def set_enabled(self, key: str, enabled: bool, actor: str, expected_version: int | None = None) -> Flag:
        return self._change(
            key, actor, "enable" if enabled else "disable", expected_version,
            lambda before: {**before.definition(), "enabled": enabled},
        )

    def delete(self, key: str, actor: str, expected_version: int | None = None) -> None:
        at = self._now()
        with self._write() as db:
            row = db.execute("SELECT * FROM flags WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise NotFound(key)
            before = self._row_to_flag(row)
            if expected_version is not None and expected_version != before.version:
                raise VersionConflict(key, expected_version, before.version)
            db.execute("DELETE FROM flags WHERE key = ?", (key,))
            self._record(db, actor, "delete", key, before.version, before, None, at)

    # ---- Import and backup ---------------------------------------------------------------

    def import_flags(self, items: list[Any], actor: str, overwrite: bool = False,
                     dry_run: bool = False) -> ImportResult:
        """Creates (and with ``overwrite``, updates) flags from snapshot data, all or nothing.

        Every flag is validated before anything is written; one bad flag means nothing
        changes. Imported changes appear in the audit log with the action ``import``.
        """
        if not isinstance(items, list):
            raise FlagError("flags", "must be a list of flags")
        incoming: list[Flag] = []
        seen: set[str] = set()
        for i, raw in enumerate(items):
            try:
                flag = Flag.from_dict(raw)
            except FlagError as exc:
                raise FlagError(f"flags[{i}].{exc.path}".rstrip("."), exc.message) from None
            if flag.key in seen:
                raise FlagError(f"flags[{i}].key", f"{flag.key!r} appears more than once")
            seen.add(flag.key)
            incoming.append(flag)

        result = ImportResult([], [], [], [])
        at = self._now()
        with self._write() as db:
            for flag in incoming:
                row = db.execute("SELECT * FROM flags WHERE key = ?", (flag.key,)).fetchone()
                if row is None:
                    result.created.append(flag.key)
                    if not dry_run:
                        db.execute(
                            "INSERT INTO flags (key, definition, version, created_at, updated_at) "
                            "VALUES (?, ?, 1, ?, ?)",
                            (flag.key, json.dumps(flag.definition()), at, at),
                        )
                        saved = Flag.from_dict({**flag.definition(), "version": 1}, key=flag.key)
                        self._record(db, actor, "import", flag.key, 1, None, saved, at)
                    continue
                before = self._row_to_flag(row)
                if before.definition() == flag.definition():
                    result.unchanged.append(flag.key)
                elif not overwrite:
                    result.skipped.append(flag.key)
                else:
                    result.updated.append(flag.key)
                    if not dry_run:
                        version = before.version + 1
                        db.execute(
                            "UPDATE flags SET definition = ?, version = ?, updated_at = ? WHERE key = ?",
                            (json.dumps(flag.definition()), version, at, flag.key),
                        )
                        after = Flag.from_dict({**flag.definition(), "version": version}, key=flag.key)
                        self._record(db, actor, "import", flag.key, version, before, after, at)
        return result

    def backup(self, destination: str | Path) -> Path:
        """Copies the whole database to ``destination`` using SQLite's online backup.

        Safe while the server is running: readers and writers just wait a moment.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            with self._lock:
                self._db.backup(target)
        finally:
            target.close()
        return destination
