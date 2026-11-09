import threading

import pytest

from flagpole.flags import FlagError
from flagpole.store import MIGRATIONS, AlreadyExists, FlagStore, NotFound, StoreError, VersionConflict


class Clock:
    def __init__(self):
        self.t = 0

    def __call__(self):
        self.t += 1
        return f"2026-11-09T00:00:{self.t:02d}.000Z"


@pytest.fixture
def store(tmp_path):
    with FlagStore(tmp_path / "flags.db", now=Clock()) as s:
        yield s


def test_create_get_list(store):
    f = store.create({"key": "dark-mode", "description": "Dark theme", "tags": ["ui"]}, actor="sam")
    assert (f.version, f.created_at, f.updated_at) == (1, "2026-11-09T00:00:01.000Z", "2026-11-09T00:00:01.000Z")
    store.create({"key": "beta-search", "tags": ["search"]}, actor="sam")
    assert store.get("dark-mode") == f
    assert store.get("nope") is None
    assert [x.key for x in store.list()] == ["beta-search", "dark-mode"]
    assert [x.key for x in store.list(tag="ui")] == ["dark-mode"]


def test_create_validates_and_refuses_duplicates(store):
    with pytest.raises(FlagError):
        store.create({"key": "Bad Key"}, actor="sam")
    store.create({"key": "x"}, actor="sam")
    with pytest.raises(AlreadyExists):
        store.create({"key": "x"}, actor="sam")
    assert store.revision() == 1  # failed writes leave no trace


def test_update_bumps_version_and_keeps_created_at(store):
    store.create({"key": "x"}, actor="sam")
    f = store.update("x", {"description": "now with words", "enabled": True}, actor="alex")
    assert (f.version, f.description, f.enabled) == (2, "now with words", True)
    assert f.created_at == "2026-11-09T00:00:01.000Z"
    assert f.updated_at == "2026-11-09T00:00:02.000Z"


def test_update_with_no_real_change_is_a_no_op(store):
    store.create({"key": "x"}, actor="sam")
    f = store.update("x", {}, actor="sam")  # same as the defaults
    assert f.version == 1
    assert store.revision() == 1
    assert len(store.audit()) == 1


def test_version_conflicts_stop_lost_updates(store):
    store.create({"key": "x"}, actor="sam")
    store.update("x", {"description": "first"}, actor="sam", expected_version=1)
    with pytest.raises(VersionConflict) as info:
        store.update("x", {"description": "second"}, actor="alex", expected_version=1)
    assert (info.value.expected, info.value.current) == (1, 2)
    assert store.get("x").description == "first"


def test_toggle_and_delete(store):
    store.create({"key": "x"}, actor="sam")
    assert store.set_enabled("x", True, actor="sam").enabled is True
    assert store.set_enabled("x", True, actor="sam").version == 2  # already on: no new version
    with pytest.raises(VersionConflict):
        store.delete("x", actor="sam", expected_version=1)
    store.delete("x", actor="sam", expected_version=2)
    assert store.get("x") is None
    for call in (lambda: store.delete("x", actor="sam"),
                 lambda: store.set_enabled("x", False, actor="sam"),
                 lambda: store.update("x", {}, actor="sam"),
                 lambda: store.require("x")):
        with pytest.raises(NotFound):
            call()


def test_invalid_update_changes_nothing(store):
    store.create({"key": "x"}, actor="sam")
    with pytest.raises(FlagError):
        store.update("x", {"default": {"variant": "missing"}}, actor="sam")
    assert store.get("x").version == 1


def test_audit_log_records_every_change_with_before_and_after(store):
    store.create({"key": "x"}, actor="sam")
    store.set_enabled("x", True, actor="alex")
    store.update("x", {"enabled": True, "description": "d"}, actor="sam")
    store.delete("x", actor="kim")
    entries = store.audit()
    assert [(e.action, e.actor, e.version) for e in entries] == [
        ("delete", "kim", 3), ("update", "sam", 3), ("enable", "alex", 2), ("create", "sam", 1),
    ]
    enable = entries[2]
    assert enable.before["enabled"] is False and enable.after["enabled"] is True
    assert entries[0].after is None and entries[-1].before is None
    assert store.revision() == 4


def test_audit_filtering_and_paging(store):
    for key in ("a", "b"):
        store.create({"key": key}, actor="sam")
        store.set_enabled(key, True, actor="sam")
    assert [e.flag for e in store.audit(flag="a")] == ["a", "a"]
    first_page = store.audit(limit=3)
    second_page = store.audit(limit=3, before_id=first_page[-1].id)
    assert len(first_page) == 3 and len(second_page) == 1
    assert {e.id for e in first_page}.isdisjoint(e.id for e in second_page)


def test_snapshot_is_consistent(store):
    store.create({"key": "a"}, actor="sam")
    store.create({"key": "b"}, actor="sam")
    revision, flags = store.snapshot()
    assert revision == 2 and [f.key for f in flags] == ["a", "b"]


def test_data_survives_reopening(tmp_path):
    path = tmp_path / "flags.db"
    with FlagStore(path) as s:
        s.create({"key": "keep-me", "enabled": True}, actor="sam")
    with FlagStore(path) as s:
        assert s.get("keep-me").enabled is True
        assert s.schema_version() == len(MIGRATIONS)


def test_refuses_a_database_from_a_newer_version(tmp_path):
    import sqlite3

    path = tmp_path / "future.db"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    db.close()
    with pytest.raises(StoreError, match="newer flagpole"):
        FlagStore(path)


def test_concurrent_writers_never_lose_updates(tmp_path):
    with FlagStore(tmp_path / "flags.db") as store:
        store.create({"key": "x"}, actor="sam")
        conflicts = []
        wins = []

        def writer(n):
            for _ in range(20):
                current = store.get("x")
                try:
                    store.update("x", {"description": f"writer {n}"}, actor=f"w{n}", expected_version=current.version)
                    wins.append(n)
                except VersionConflict:
                    conflicts.append(n)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = store.get("x")
        # Every successful write produced exactly one version and one audit entry.
        changed = [e for e in store.audit(flag="x", limit=500) if e.action == "update"]
        assert final.version == 1 + len(changed)
        assert len(changed) <= len(wins)


def test_memory_database_works():
    with FlagStore(":memory:") as s:
        s.create({"key": "x"}, actor="t")
        assert s.get("x").version == 1
