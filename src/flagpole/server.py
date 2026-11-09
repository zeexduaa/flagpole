"""The HTTP API, built on the standard library's threading HTTP server.

Authentication uses two bearer tokens:

* the **admin** token can do everything (manage flags, read the audit log);
* the **client** token, for your apps, can only download the snapshot and evaluate.

Endpoints (all JSON):

    GET    /health                         no auth
    GET    /api/flags[?tag=ui]             admin
    POST   /api/flags                      admin   create
    GET    /api/flags/{key}                admin   ETag: "<version>"
    PUT    /api/flags/{key}                admin   replace; If-Match: "<version>" optional
    POST   /api/flags/{key}/enable         admin   If-Match optional
    POST   /api/flags/{key}/disable        admin   If-Match optional
    DELETE /api/flags/{key}                admin   If-Match optional
    GET    /api/audit[?flag=&limit=&before=]  admin
    GET    /api/snapshot                   client  ETag: "r<revision>"; If-None-Match -> 304
    POST   /api/evaluate                   client  {"context": {...}, "flags": ["a", "b"]?}
"""

from __future__ import annotations

import hmac
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .flags import Evaluation, FlagError
from .store import AlreadyExists, FlagStore, NotFound, VersionConflict

MAX_BODY = 256 * 1024
ACTOR_PATTERN = re.compile(r"^[\w.@+-]{1,64}$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, path: str | None = None,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.path = status, code, message, path
        self.headers = headers or {}


@dataclass
class Response:
    status: int = 200
    body: Any = None
    headers: dict[str, str] | None = None


@dataclass(frozen=True)
class Tokens:
    admin: str
    client: str

    def __post_init__(self) -> None:
        for name, value in (("admin", self.admin), ("client", self.client)):
            if len(value) < 16:
                raise ValueError(f"the {name} token must be at least 16 characters")
        if hmac.compare_digest(self.admin, self.client):
            raise ValueError("the admin and client tokens must be different")


Handler = Callable[["Request"], Response]


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: Any
    body: bytes
    role: str = "none"
    actor: str = "anonymous"
    params: dict[str, str] | None = None

    def json(self) -> Any:
        if not self.body:
            raise ApiError(400, "invalid_json", "this request needs a JSON body")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(415, "unsupported_media_type", "send JSON with Content-Type: application/json")
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "invalid_json", f"the body isn't valid JSON: {exc}") from None

    def if_match(self) -> int | None:
        """The version from an If-Match header like "3" (quotes optional); None if absent."""
        raw = self.headers.get("If-Match")
        if raw is None:
            return None
        value = raw.strip().removeprefix("W/").strip('"')
        if not value.isdigit():
            raise ApiError(400, "invalid_if_match", 'If-Match must be the flag version, e.g. "3"')
        return int(value)


class Api:
    """Routing and request handling, independent of the socket server (easy to test)."""

    def __init__(self, store: FlagStore, tokens: Tokens):
        self.store = store
        self.tokens = tokens
        self.routes: list[tuple[str, re.Pattern[str], str, Handler]] = []
        key = r"(?P<key>[a-z0-9][a-z0-9._-]{0,63})"
        self._add("GET", r"/health", "none", self.health)
        self._add("GET", r"/api/flags", "admin", self.list_flags)
        self._add("POST", r"/api/flags", "admin", self.create_flag)
        self._add("GET", rf"/api/flags/{key}", "admin", self.get_flag)
        self._add("PUT", rf"/api/flags/{key}", "admin", self.replace_flag)
        self._add("DELETE", rf"/api/flags/{key}", "admin", self.delete_flag)
        self._add("POST", rf"/api/flags/{key}/enable", "admin", lambda r: self.set_enabled(r, True))
        self._add("POST", rf"/api/flags/{key}/disable", "admin", lambda r: self.set_enabled(r, False))
        self._add("GET", r"/api/audit", "admin", self.audit)
        self._add("GET", r"/api/snapshot", "client", self.snapshot)
        self._add("POST", r"/api/evaluate", "client", self.evaluate)

    def _add(self, method: str, pattern: str, role: str, handler: Handler) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), role, handler))

    # ---- Dispatch ------------------------------------------------------------------------

    def _role_for(self, headers: Any) -> str:
        auth = headers.get("Authorization") or ""
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return "none"
        token = token.strip()
        if hmac.compare_digest(token, self.tokens.admin):
            return "admin"
        if hmac.compare_digest(token, self.tokens.client):
            return "client"
        return "invalid"

    def handle(self, request: Request) -> Response:
        try:
            allowed = []
            for method, pattern, required, handler in self.routes:
                match = pattern.match(request.path)
                if not match:
                    continue
                if method != request.method:
                    allowed.append(method)
                    continue
                request.params = {k: unquote(v) for k, v in match.groupdict().items()}
                request.role = self._role_for(request.headers)
                self._authorize(request, required)
                return handler(request)
            if allowed:
                raise ApiError(405, "method_not_allowed", f"use {', '.join(sorted(set(allowed)))} here",
                               headers={"Allow": ", ".join(sorted(set(allowed)))})
            raise ApiError(404, "not_found", f"no endpoint at {request.path}")
        except ApiError as exc:
            return self._error(exc)
        except FlagError as exc:
            return self._error(ApiError(422, "invalid_flag", exc.message, exc.path))
        except NotFound as exc:
            return self._error(ApiError(404, "flag_not_found", str(exc)))
        except AlreadyExists as exc:
            return self._error(ApiError(409, "flag_exists", str(exc)))
        except VersionConflict as exc:
            return self._error(ApiError(412, "version_conflict", str(exc), headers={"ETag": f'"{exc.current}"'}))

    def _authorize(self, request: Request, required: str) -> None:
        if required == "none":
            return
        if request.role in ("none", "invalid"):
            raise ApiError(401, "unauthorized", "send a valid token: Authorization: Bearer <token>",
                           headers={"WWW-Authenticate": 'Bearer realm="flagpole"'})
        if required == "admin" and request.role != "admin":
            raise ApiError(403, "forbidden", "this needs the admin token; the client token can only read and evaluate")
        actor = (request.headers.get("X-Flagpole-Actor") or "").strip()
        if actor and not ACTOR_PATTERN.match(actor):
            raise ApiError(400, "invalid_actor", "X-Flagpole-Actor: use up to 64 letters, digits or . _ @ + -")
        request.actor = actor or request.role

    @staticmethod
    def _error(exc: ApiError) -> Response:
        error: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.path is not None:
            error["path"] = exc.path
        return Response(exc.status, {"error": error}, exc.headers)

    # ---- Handlers ------------------------------------------------------------------------

    def health(self, request: Request) -> Response:
        return Response(200, {"status": "ok", "version": __version__, "revision": self.store.revision()})

    def list_flags(self, request: Request) -> Response:
        tag = request.query.get("tag")
        revision, flags = self.store.snapshot()
        if tag:
            flags = [f for f in flags if tag in f.tags]
        return Response(200, {"revision": revision, "flags": [f.to_dict() for f in flags]})

    def create_flag(self, request: Request) -> Response:
        data = request.json()
        flag = self.store.create(data, actor=request.actor)
        return Response(201, flag.to_dict(), {"Location": f"/api/flags/{flag.key}", "ETag": f'"{flag.version}"'})

    def get_flag(self, request: Request) -> Response:
        assert request.params is not None
        flag = self.store.require(request.params["key"])
        return Response(200, flag.to_dict(), {"ETag": f'"{flag.version}"'})

    def replace_flag(self, request: Request) -> Response:
        assert request.params is not None
        data = request.json()
        if not isinstance(data, dict):
            raise ApiError(422, "invalid_flag", "a flag must be a JSON object")
        flag = self.store.update(request.params["key"], data, actor=request.actor, expected_version=request.if_match())
        return Response(200, flag.to_dict(), {"ETag": f'"{flag.version}"'})

    def set_enabled(self, request: Request, enabled: bool) -> Response:
        assert request.params is not None
        flag = self.store.set_enabled(request.params["key"], enabled, actor=request.actor,
                                      expected_version=request.if_match())
        return Response(200, flag.to_dict(), {"ETag": f'"{flag.version}"'})

    def delete_flag(self, request: Request) -> Response:
        assert request.params is not None
        self.store.delete(request.params["key"], actor=request.actor, expected_version=request.if_match())
        return Response(204)

    def audit(self, request: Request) -> Response:
        def number(name: str, default: int | None) -> int | None:
            raw = request.query.get(name)
            if raw is None:
                return default
            if not raw.isdigit():
                raise ApiError(400, "invalid_query", f"'{name}' must be a whole number")
            return int(raw)

        entries = self.store.audit(flag=request.query.get("flag"), limit=number("limit", 50) or 50,
                                   before_id=number("before", None))
        body: dict[str, Any] = {"entries": [e.to_dict() for e in entries]}
        if entries:
            body["next_before"] = entries[-1].id
        return Response(200, body)

    def snapshot(self, request: Request) -> Response:
        revision, flags = self.store.snapshot()
        etag = f'"r{revision}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(304, None, {"ETag": etag})
        return Response(200, {"revision": revision, "flags": [f.to_dict() for f in flags]},
                        {"ETag": etag, "Cache-Control": "no-cache"})

    def evaluate(self, request: Request) -> Response:
        data = request.json()
        if not isinstance(data, dict) or not isinstance(data.get("context", {}), dict):
            raise ApiError(422, "invalid_request", 'send {"context": {...}} with an optional "flags" list')
        context = data.get("context", {})
        wanted = data.get("flags")
        if wanted is not None and (not isinstance(wanted, list) or not all(isinstance(k, str) for k in wanted)):
            raise ApiError(422, "invalid_request", "'flags' must be a list of flag keys", "flags")
        _, flags = self.store.snapshot()
        by_key = {f.key: f for f in flags}
        keys = wanted if wanted is not None else list(by_key)
        results = {}
        for key in keys:
            flag = by_key.get(key)
            if flag is None:
                results[key] = {"flag": key, "variant": None, "value": None, "reason": "flag_not_found"}
            else:
                result: Evaluation = flag.evaluate(context)
                results[key] = result.to_dict()
        return Response(200, {"results": results})


# ---- Socket server -------------------------------------------------------------------------


def make_handler(api: Api, log: Callable[[str], None]) -> type[BaseHTTPRequestHandler]:
    class RequestHandler(BaseHTTPRequestHandler):
        server_version = f"flagpole/{__version__}"
        protocol_version = "HTTP/1.1"

        def _serve(self) -> None:
            started = time.perf_counter()
            split = urlsplit(self.path)
            length_header = self.headers.get("Content-Length", "0")
            if not length_header.isdigit():
                response = Api._error(ApiError(400, "bad_request", "invalid Content-Length"))
            elif int(length_header) > MAX_BODY:
                response = Api._error(ApiError(413, "body_too_large", f"bodies are limited to {MAX_BODY // 1024} KB"))
                self.close_connection = True
            else:
                body = self.rfile.read(int(length_header)) if int(length_header) else b""
                query = {k: v[0] for k, v in parse_qs(split.query).items()}
                request = Request(self.command, split.path.rstrip("/") or "/", query, self.headers, body)
                try:
                    response = api.handle(request)
                except Exception as exc:  # last resort: never leak a traceback to the client
                    log(f"internal error on {self.command} {split.path}: {exc!r}")
                    response = Api._error(ApiError(500, "internal_error", "something went wrong on the server"))
            self._send(response)
            elapsed = (time.perf_counter() - started) * 1000
            log(f"{self.command} {split.path} {response.status} {elapsed:.1f}ms")

        def _send(self, response: Response) -> None:
            payload = b"" if response.body is None else json.dumps(response.body, separators=(",", ":")).encode()
            self.send_response(response.status)
            headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
            headers.update(response.headers or {})
            for name, value in headers.items():
                self.send_header(name, value)
            if payload:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload and self.command != "HEAD":
                self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        def log_message(self, format: str, *args: Any) -> None:
            pass  # replaced by our own one-line access log in _serve

    return RequestHandler


def create_server(store: FlagStore, tokens: Tokens, host: str = "127.0.0.1", port: int = 8080,
                  log: Callable[[str], None] | None = None) -> ThreadingHTTPServer:
    def default_log(line: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {line}", file=sys.stderr, flush=True)

    server = ThreadingHTTPServer((host, port), make_handler(Api(store, tokens), log or default_log))
    server.daemon_threads = True
    return server
