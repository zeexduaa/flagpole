from conftest import ADMIN, CLIENT
from flagpole.client import FlagpoleClient


def test_client_loads_and_evaluates_locally(live):
    live.call("POST", "/api/flags", {"key": "dark-mode", "enabled": True})
    live.call("POST", "/api/flags", {
        "key": "button-color", "enabled": True,
        "variants": {"blue": "#2563eb", "green": "#16a34a"},
        "default": {"rollout": {"blue": 50, "green": 50}},
    })
    client = FlagpoleClient(live.url, CLIENT, refresh_interval=0)
    assert client.ready is False
    assert client.is_enabled("dark-mode", default=True) is True  # not loaded yet: your default
    assert client.evaluate("dark-mode").reason == "client_not_ready"

    assert client.refresh() is True
    assert client.ready and client.revision == 2
    assert client.is_enabled("dark-mode") is True
    color = client.value("button-color", {"key": "user-1"})
    assert color in ("#2563eb", "#16a34a")
    assert client.variant("button-color", {"key": "user-1"}) in ("blue", "green")
    # Local evaluation matches the server's answer exactly.
    server = live.call("POST", "/api/evaluate", {"context": {"key": "user-1"}}, token=CLIENT)[1]["results"]
    assert client.evaluate("button-color", {"key": "user-1"}).to_dict() == server["button-color"]


def test_unknown_flags_use_the_default(live):
    client = FlagpoleClient(live.url, CLIENT, refresh_interval=0)
    client.refresh()
    result = client.evaluate("ghost", default="fallback")
    assert (result.reason, result.value) == ("flag_not_found", "fallback")
    assert client.value("ghost", default=3) == 3
    assert client.is_enabled("ghost") is False


def test_refresh_uses_etags(live):
    live.call("POST", "/api/flags", {"key": "x"})
    client = FlagpoleClient(live.url, CLIENT, refresh_interval=0)
    assert client.refresh() is True
    assert client.refresh() is False  # 304: nothing changed
    live.call("POST", "/api/flags/x/enable", token=ADMIN)
    assert client.refresh() is True
    assert client.is_enabled("x")
    assert any("/api/snapshot 304" in line for line in live.log)


def test_server_down_keeps_last_snapshot_and_reports(live):
    live.call("POST", "/api/flags", {"key": "x", "enabled": True})
    errors = []
    client = FlagpoleClient(live.url, CLIENT, refresh_interval=0, timeout=1, on_error=errors.append)
    client.refresh()
    live.server.shutdown()
    live.server.server_close()
    assert client.refresh() is False
    assert client.last_error is not None and errors
    assert client.is_enabled("x") is True  # still serving the last good copy


def test_wrong_token_is_reported_not_raised(live):
    client = FlagpoleClient(live.url, "not-the-right-token-123", refresh_interval=0)
    assert client.refresh() is False
    assert "401" in str(client.last_error)
    assert client.is_enabled("anything", default=True) is True


def test_background_refresh(live):
    import time

    live.call("POST", "/api/flags", {"key": "x"})
    with FlagpoleClient(live.url, CLIENT, refresh_interval=0.05) as client:
        assert client.is_enabled("x") is False
        live.call("POST", "/api/flags/x/enable", token=ADMIN)
        deadline = time.time() + 3
        while not client.is_enabled("x") and time.time() < deadline:
            time.sleep(0.02)
        assert client.is_enabled("x")


def test_offline_snapshot_and_unreadable_flags():
    errors = []
    client = FlagpoleClient("http://unused", CLIENT, on_error=errors.append)
    client.load_snapshot({"revision": 7, "flags": [
        {"key": "ok", "enabled": True},
        {"key": "from-the-future", "variants": {"a": 1, "b": 2}, "default": {"variant": "c"}},
    ]})
    assert client.revision == 7 and client.is_enabled("ok")
    assert client.evaluate("from-the-future").reason == "flag_not_found"
    assert len(errors) == 1
