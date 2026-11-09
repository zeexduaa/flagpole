import json
import threading
import urllib.error
import urllib.request

import pytest

from flagpole.server import Tokens, create_server
from flagpole.store import FlagStore

ADMIN = "admin-token-for-tests-0001"
CLIENT = "client-token-for-tests-0001"


class LiveServer:
    def __init__(self, store, server):
        self.store = store
        self.server = server
        host, port = server.server_address[:2]
        self.url = f"http://{host}:{port}"
        self.log = []

    def call(self, method, path, body=None, token=ADMIN, headers=None, raw=None):
        """Returns (status, json_or_None, headers)."""
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        all_headers = dict(headers or {})
        if token:
            all_headers["Authorization"] = f"Bearer {token}"
        if data is not None and "Content-Type" not in all_headers:
            all_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload else None, response.headers
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            return exc.code, json.loads(payload) if payload else None, exc.headers


@pytest.fixture
def live(tmp_path):
    store = FlagStore(tmp_path / "flags.db")
    log = []
    server = create_server(store, Tokens(ADMIN, CLIENT), "127.0.0.1", 0, log=log.append)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    live = LiveServer(store, server)
    live.log = log
    yield live
    server.shutdown()
    server.server_close()
    store.close()
