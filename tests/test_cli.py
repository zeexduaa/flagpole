import json
import threading
import time
import urllib.request

import pytest

from flagpole.cli import load_dotenv, main


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep any real .env out of the way
    return str(tmp_path / "cli.db")


def run(db, *args):
    return main(["--db", db, *args])


def test_create_list_enable_show(db, capsys):
    assert run(db, "flags", "create", "dark-mode", "--description", "Dark theme", "--actor", "sam") == 0
    assert "Turn it on with: flagpole flags enable dark-mode" in capsys.readouterr().out
    assert run(db, "flags", "enable", "dark-mode", "--actor", "sam") == 0
    assert "dark-mode is now ON (version 2)" in capsys.readouterr().out
    assert run(db, "flags", "list") == 0
    assert "ON   dark-mode" in capsys.readouterr().out
    assert run(db, "flags", "show", "dark-mode") == 0
    assert json.loads(capsys.readouterr().out)["description"] == "Dark theme"


def test_create_and_update_from_json(db, tmp_path, capsys):
    spec = tmp_path / "flag.json"
    spec.write_text(json.dumps({"enabled": True, "variants": {"a": 1, "b": 2}, "default": {"variant": "b"}}))
    assert run(db, "flags", "create", "ab", "--json", str(spec)) == 0
    assert run(db, "eval", "ab", "--context", '{"key": "u1"}') == 0
    out = capsys.readouterr().out
    assert json.loads(out[out.index("{"):])["value"] == 2
    spec.write_text(json.dumps({"enabled": True, "variants": {"a": 1, "b": 2}, "default": {"variant": "a"}}))
    assert run(db, "flags", "update", "ab", "--json", str(spec), "--if-version", "1") == 0
    assert run(db, "flags", "update", "ab", "--json", str(spec), "--if-version", "2") == 0  # same again: no-op
    spec.write_text(json.dumps({"enabled": False, "variants": {"a": 1, "b": 2}}))
    assert run(db, "flags", "update", "ab", "--json", str(spec), "--if-version", "1") == 1
    assert "changed by someone else" in capsys.readouterr().err


def test_invalid_flags_and_missing_flags(db, capsys):
    assert run(db, "flags", "create", "Bad Key") == 2
    assert "invalid flag: key:" in capsys.readouterr().err
    assert run(db, "flags", "enable", "ghost") == 1
    assert "not found" in capsys.readouterr().err


def test_delete_needs_confirmation(db, capsys, monkeypatch):
    run(db, "flags", "create", "x")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="--yes"):
        run(db, "flags", "delete", "x")
    assert run(db, "flags", "delete", "x", "--yes") == 0


def test_audit_and_export(db, capsys):
    run(db, "flags", "create", "x", "--actor", "sam")
    run(db, "flags", "enable", "x", "--actor", "kim")
    capsys.readouterr()
    assert run(db, "audit") == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "kim" in lines[0] and "enable" in lines[0] and "sam" in lines[1]
    assert run(db, "export") == 0
    snapshot = json.loads(capsys.readouterr().out)
    assert snapshot["revision"] == 2 and snapshot["flags"][0]["enabled"] is True


def test_serve_requires_tokens(db, capsys, monkeypatch):
    monkeypatch.delenv("FLAGPOLE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("FLAGPOLE_CLIENT_TOKEN", raising=False)
    assert run(db, "serve") == 2
    assert "--dev" in capsys.readouterr().err


def test_serve_dev_mode_starts_a_working_server(db, capsys, monkeypatch):
    import socket

    monkeypatch.delenv("FLAGPOLE_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("FLAGPOLE_CLIENT_TOKEN", raising=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    thread = threading.Thread(target=run, args=(db, "serve", "--dev", "--port", str(port)), daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                assert json.loads(r.read())["status"] == "ok"
                break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail("dev server didn't start")
    assert "dev admin token:" in capsys.readouterr().out


def test_load_dotenv(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# c\nFLAGPOLE_ADMIN_TOKEN="abc"\nexport FLAGPOLE_DB=x.db\nEMPTY=\nKEEP=file\n')
    env = {"KEEP": "shell"}
    assert load_dotenv(str(path), env) == 2
    assert env == {"KEEP": "shell", "FLAGPOLE_ADMIN_TOKEN": "abc", "FLAGPOLE_DB": "x.db"}
