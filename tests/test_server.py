import pytest

from conftest import ADMIN, CLIENT
from flagpole.server import Tokens


def test_health_needs_no_token(live):
    status, body, headers = live.call("GET", "/health", token=None)
    assert status == 200 and body["status"] == "ok" and body["revision"] == 0
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_create_get_and_list(live):
    status, body, headers = live.call("POST", "/api/flags", {"key": "dark-mode", "tags": ["ui"]})
    assert status == 201
    assert headers["Location"] == "/api/flags/dark-mode" and headers["ETag"] == '"1"'
    assert body["version"] == 1 and body["enabled"] is False
    status, body, headers = live.call("GET", "/api/flags/dark-mode")
    assert status == 200 and body["key"] == "dark-mode" and headers["ETag"] == '"1"'
    live.call("POST", "/api/flags", {"key": "search-v2", "tags": ["search"]})
    status, body, _ = live.call("GET", "/api/flags?tag=ui")
    assert [f["key"] for f in body["flags"]] == ["dark-mode"] and body["revision"] == 2


def test_tokens_and_roles(live):
    assert live.call("GET", "/api/flags", token=None)[0] == 401
    status, body, headers = live.call("GET", "/api/flags", token="wrong-token-wrong-token")
    assert status == 401 and body["error"]["code"] == "unauthorized"
    assert headers["WWW-Authenticate"].startswith("Bearer")
    status, body, _ = live.call("GET", "/api/flags", token=CLIENT)
    assert status == 403 and "admin token" in body["error"]["message"]
    assert live.call("GET", "/api/snapshot", token=CLIENT)[0] == 200
    assert live.call("GET", "/api/snapshot", token=ADMIN)[0] == 200  # admin can do anything


def test_validation_errors_point_at_the_field(live):
    status, body, _ = live.call("POST", "/api/flags", {"key": "x", "default": {"rollout": {"on": 70, "off": 20}}})
    assert status == 422
    assert body["error"] == {"code": "invalid_flag", "message": "percentages must add up to 100, not 90",
                             "path": "default.rollout"}


def test_bad_requests(live):
    assert live.call("POST", "/api/flags", raw=b"{nope")[1]["error"]["code"] == "invalid_json"
    status, body, _ = live.call("POST", "/api/flags", raw=b'{"key": "x"}', headers={"Content-Type": "text/plain"})
    assert status == 415
    assert live.call("POST", "/api/flags")[1]["error"]["code"] == "invalid_json"  # no body
    status, body, headers = live.call("PATCH", "/api/flags")
    assert status == 405 and headers["Allow"] == "GET, POST"
    assert live.call("GET", "/api/nothing-here")[0] == 404
    assert live.call("GET", "/api/flags/Bad%20Key")[0] == 404
    status, body, _ = live.call("POST", "/api/flags", raw=b"x" * (256 * 1024 + 1))
    assert status == 413


def test_duplicate_and_missing_flags(live):
    live.call("POST", "/api/flags", {"key": "x"})
    assert live.call("POST", "/api/flags", {"key": "x"})[1]["error"]["code"] == "flag_exists"
    status, body, _ = live.call("GET", "/api/flags/ghost")
    assert status == 404 and body["error"]["code"] == "flag_not_found"


def test_replace_with_if_match(live):
    live.call("POST", "/api/flags", {"key": "x"})
    status, body, headers = live.call("PUT", "/api/flags/x", {"description": "v2"}, headers={"If-Match": '"1"'})
    assert status == 200 and body["version"] == 2 and headers["ETag"] == '"2"'
    status, body, headers = live.call("PUT", "/api/flags/x", {"description": "stale"}, headers={"If-Match": '"1"'})
    assert status == 412 and body["error"]["code"] == "version_conflict" and headers["ETag"] == '"2"'
    assert live.call("PUT", "/api/flags/x", {"description": "x"}, headers={"If-Match": "abc"})[0] == 400
    assert live.call("PUT", "/api/flags/x", {"key": "y"})[1]["error"]["path"] == "key"
    assert live.call("PUT", "/api/flags/x", ["not", "an", "object"])[0] == 422


def test_enable_disable_delete(live):
    live.call("POST", "/api/flags", {"key": "x"})
    assert live.call("POST", "/api/flags/x/enable")[1]["enabled"] is True
    assert live.call("POST", "/api/flags/x/disable", headers={"If-Match": '"1"'})[0] == 412
    assert live.call("POST", "/api/flags/x/disable")[1]["enabled"] is False
    assert live.call("DELETE", "/api/flags/x", headers={"If-Match": '"3"'})[0] == 204
    assert live.call("GET", "/api/flags/x")[0] == 404


def test_audit_records_the_actor(live):
    live.call("POST", "/api/flags", {"key": "x"}, headers={"X-Flagpole-Actor": "sam@example.com"})
    live.call("POST", "/api/flags/x/enable")
    status, body, _ = live.call("GET", "/api/audit?flag=x&limit=1")
    assert status == 200
    assert [(e["action"], e["actor"]) for e in body["entries"]] == [("enable", "admin")]
    older = live.call("GET", f"/api/audit?before={body['next_before']}")[1]["entries"]
    assert [(e["action"], e["actor"]) for e in older] == [("create", "sam@example.com")]
    assert live.call("GET", "/api/audit?limit=lots")[0] == 400
    assert live.call("POST", "/api/flags", {"key": "y"}, headers={"X-Flagpole-Actor": "bad actor!"})[0] == 400


def test_snapshot_etag_and_not_modified(live):
    live.call("POST", "/api/flags", {"key": "x"})
    status, body, headers = live.call("GET", "/api/snapshot", token=CLIENT)
    assert status == 200 and body["revision"] == 1 and headers["ETag"] == '"r1"'
    status, body, _ = live.call("GET", "/api/snapshot", token=CLIENT, headers={"If-None-Match": '"r1"'})
    assert status == 304 and body is None
    live.call("POST", "/api/flags/x/enable")
    assert live.call("GET", "/api/snapshot", token=CLIENT, headers={"If-None-Match": '"r1"'})[0] == 200


def test_evaluate(live):
    live.call("POST", "/api/flags", {
        "key": "eu-banner", "enabled": True, "default": {"variant": "off"},
        "rules": [{"conditions": [{"attribute": "country", "op": "in", "value": ["DE", "FR"]}],
                   "serve": {"variant": "on"}}],
    })
    live.call("POST", "/api/flags", {"key": "dark-mode"})
    status, body, _ = live.call("POST", "/api/evaluate", {"context": {"key": "u1", "country": "DE"}}, token=CLIENT)
    assert status == 200
    assert body["results"]["eu-banner"] == {"flag": "eu-banner", "variant": "on", "value": True,
                                            "reason": "rule_match", "rule": 0, "version": 1}
    assert body["results"]["dark-mode"]["reason"] == "disabled"
    body = live.call("POST", "/api/evaluate", {"context": {}, "flags": ["eu-banner", "ghost"]}, token=CLIENT)[1]
    assert body["results"]["ghost"]["reason"] == "flag_not_found"
    assert set(body["results"]) == {"eu-banner", "ghost"}
    assert live.call("POST", "/api/evaluate", {"context": "nope"}, token=CLIENT)[0] == 422
    assert live.call("POST", "/api/evaluate", {"flags": "eu-banner"}, token=CLIENT)[0] == 422


def test_access_log_never_contains_tokens(live):
    live.call("GET", "/api/flags")
    assert live.log and all(ADMIN not in line and CLIENT not in line for line in live.log)
    assert any(line.startswith("GET /api/flags 200") for line in live.log)


def test_tokens_must_be_strong_and_different():
    with pytest.raises(ValueError, match="at least 16"):
        Tokens("short", CLIENT)
    with pytest.raises(ValueError, match="different"):
        Tokens(ADMIN, ADMIN)
