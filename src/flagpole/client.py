"""Python SDK: keep a local copy of all flags and evaluate them in-process.

    from flagpole.client import FlagpoleClient

    flags = FlagpoleClient("http://localhost:8080", token="<client token>")
    flags.start()                                    # first download + background refresh
    if flags.is_enabled("new-checkout", {"key": user.id, "country": user.country}):
        ...

Checking a flag never makes a network call and never raises: it uses the last
snapshot it downloaded and falls back to the default you pass if the flag is
unknown or the client hasn't loaded yet. Refreshes ask the server "changed since
revision N?" (If-None-Match), so an idle poll costs one tiny request.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from . import __version__
from .flags import Evaluation, Flag, FlagError


class FlagpoleClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        refresh_interval: float = 30.0,
        timeout: float = 5.0,
        on_error: Callable[[Exception], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.refresh_interval = refresh_interval
        self.timeout = timeout
        self._on_error = on_error
        self._flags: dict[str, Flag] = {}
        self._revision: int | None = None
        self._etag: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: Exception | None = None

    # ---- Loading -------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """True once a snapshot has been loaded."""
        return self._revision is not None

    @property
    def revision(self) -> int | None:
        return self._revision

    def load_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """Uses a snapshot you already have (e.g. bundled with your app for offline starts)."""
        flags = {}
        for raw in snapshot.get("flags", []):
            try:
                flag = Flag.from_dict(raw)
            except FlagError as exc:
                self._report(exc)  # skip a flag this SDK version can't read; keep the rest
                continue
            flags[flag.key] = flag
        with self._lock:
            self._flags = flags
            self._revision = int(snapshot.get("revision", 0))

    def refresh(self) -> bool:
        """Downloads the snapshot if it changed. Returns True if new flags were loaded.

        Errors are recorded in ``last_error`` (and passed to ``on_error``); the last
        good snapshot stays in use.
        """
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": f"flagpole-python/{__version__}"}
        if self._etag:
            headers["If-None-Match"] = self._etag
        request = urllib.request.Request(f"{self.base_url}/api/snapshot", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                snapshot = json.loads(response.read())
                etag = response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                self.last_error = None
                return False
            self._report(RuntimeError(f"flagpole server answered HTTP {exc.code}"))
            return False
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self._report(exc)
            return False
        self.load_snapshot(snapshot)
        self._etag = etag
        self.last_error = None
        return True

    def start(self) -> FlagpoleClient:
        """Loads once now, then keeps refreshing in a background thread."""
        self.refresh()
        if self._thread is None and self.refresh_interval > 0:
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll, name="flagpole-refresh", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.timeout + 1)
            self._thread = None

    def __enter__(self) -> FlagpoleClient:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _poll(self) -> None:
        while not self._stop.wait(self.refresh_interval):
            self.refresh()

    def _report(self, exc: Exception) -> None:
        self.last_error = exc
        if self._on_error is not None:
            try:
                self._on_error(exc)
            except Exception:
                pass

    # ---- Evaluating ----------------------------------------------------------------------

    def evaluate(self, key: str, context: Mapping[str, Any] | None = None, default: Any = None) -> Evaluation:
        """Full result (variant, value, reason). Never raises."""
        with self._lock:
            flag = self._flags.get(key)
            loaded = self._revision is not None
        if flag is None:
            reason = "flag_not_found" if loaded else "client_not_ready"
            return Evaluation(key, "", default, reason)
        try:
            return flag.evaluate(context or {})
        except Exception as exc:  # a bug in a rule must never take the app down
            self._report(exc)
            return Evaluation(key, "", default, "error")

    def value(self, key: str, context: Mapping[str, Any] | None = None, default: Any = None) -> Any:
        result = self.evaluate(key, context, default)
        return result.value if result.variant else default

    def variant(self, key: str, context: Mapping[str, Any] | None = None, default: str | None = None) -> str | None:
        result = self.evaluate(key, context)
        return result.variant or default

    def is_enabled(self, key: str, context: Mapping[str, Any] | None = None, default: bool = False) -> bool:
        """For on/off flags: True only when the served value is exactly ``true``."""
        result = self.evaluate(key, context, default)
        return result.value is True if result.variant else default
