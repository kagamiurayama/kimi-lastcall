"""Loopback-only authenticated HTTP surface for the local control panel."""

from __future__ import annotations

from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .config import ControllerConfig, ensure_control_token
from .controller import Controller, ControllerError


MAX_BODY_BYTES = 32 * 1024
STATIC_TYPES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], controller: Controller, token: str) -> None:
        super().__init__(address, ControlRequestHandler)
        self.controller = controller
        self.control_token = token
        self.expected_host = "%s:%d" % address


class ControlRequestHandler(BaseHTTPRequestHandler):
    server: ControlHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # The login token may arrive in the query string. Never place request
        # targets in logs; operators get explicit status/error JSON instead.
        return

    def _send(self, status: int, body: bytes, content_type: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        self._send(
            status,
            (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _host_valid(self) -> bool:
        host = str(self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in {"127.0.0.1:%d" % port, "localhost:%d" % port}

    def _auth_mode(self) -> Optional[str]:
        if not self._host_valid():
            return None
        authorization = str(self.headers.get("Authorization") or "")
        if authorization.startswith("Bearer ") and hmac.compare_digest(
            authorization[7:].strip(), self.server.control_token
        ):
            return "bearer"
        jar = cookies.SimpleCookie()
        try:
            jar.load(str(self.headers.get("Cookie") or ""))
        except cookies.CookieError:
            return None
        morsel = jar.get("kimi_lastcall_token")
        if morsel and hmac.compare_digest(morsel.value, self.server.control_token):
            return "cookie"
        return None

    def _same_origin_for_cookie_write(self, mode: Optional[str]) -> bool:
        if mode != "cookie":
            return True
        origin = str(self.headers.get("Origin") or "")
        port = self.server.server_address[1]
        return origin in {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port}

    def _login(self, parsed: Any) -> bool:
        supplied = parse_qs(parsed.query, keep_blank_values=True).get("token", [""])[0]
        if supplied and hmac.compare_digest(supplied, self.server.control_token) and self._host_valid():
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                "kimi_lastcall_token=%s; Path=/; HttpOnly; SameSite=Strict" % self.server.control_token,
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return True
        return False

    def _static(self, path: str) -> None:
        resource = STATIC_TYPES.get(path)
        if resource is None:
            self._json(404, {"ok": False, "error": "not_found"})
            return
        name, content_type = resource
        file_path = Path(__file__).resolve().parent / "web" / name
        try:
            body = file_path.read_bytes()
        except OSError:
            self._json(500, {"ok": False, "error": "static_asset_missing"})
            return
        self._send(200, body, content_type)

    def _body(self) -> Dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise ControllerError("request_length_invalid") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ControllerError("request_body_too_large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") if raw else "{}")
        except (UnicodeError, ValueError) as exc:
            raise ControllerError("request_json_invalid") from exc
        if not isinstance(payload, dict):
            raise ControllerError("request_json_not_object")
        return payload

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/" and parsed.query and self._login(parsed):
            return
        if self._auth_mode() is None:
            self._json(401, {"ok": False, "error": "authentication_required"})
            return
        if parsed.path == "/api/v1/status":
            try:
                self._json(200, {"ok": True, "status": self.server.controller.status()})
            except ControllerError as exc:
                self._json(409, {"ok": False, "error": str(exc)})
            return
        self._static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        mode = self._auth_mode()
        if mode is None:
            self._json(401, {"ok": False, "error": "authentication_required"})
            return
        if not self._same_origin_for_cookie_write(mode):
            self._json(403, {"ok": False, "error": "origin_rejected"})
            return
        try:
            body = self._body()
            if parsed.path == "/api/v1/settings":
                result = self.server.controller.update_settings(body)
            elif parsed.path == "/api/v1/switch/preview":
                if body:
                    raise ControllerError("preview_body_must_be_empty")
                result = self.server.controller.preview()
            elif parsed.path == "/api/v1/switch/confirm":
                result = self.server.controller.confirm(body)
            elif parsed.path == "/api/v1/adopt":
                if mode != "bearer":
                    raise ControllerError("adoption_requires_bearer")
                result = self.server.controller.adopt(body)
            elif parsed.path == "/api/v1/auto-handoff":
                if mode != "bearer":
                    raise ControllerError("auto_handoff_requires_bearer")
                result = self.server.controller.queue_auto_handoff(body)
                self._json(202, {"ok": True, "result": result})
                self.wfile.flush()
                # The hook has received the accepted response before this
                # worker exists. This prevents /new from re-entering the Stop
                # request that queued it.
                self.server.controller.start_auto_handoff_worker(
                    str(result.get("request_id") or ""),
                    str(body.get("session_id") or ""),
                )
                return
            else:
                self._json(404, {"ok": False, "error": "not_found"})
                return
        except ControllerError as exc:
            self._json(409, {"ok": False, "error": str(exc)})
            return
        self._json(200, {"ok": True, "result": result})


def make_server(config: ControllerConfig, *, controller: Optional[Controller] = None) -> ControlHTTPServer:
    token = ensure_control_token()
    active_controller = controller or Controller(config)
    active_controller.reconcile_pending()
    server = ControlHTTPServer(
        (config.host, config.port),
        active_controller,
        token,
    )
    active_controller.resume_auto_handoff()
    return server


def serve(config: ControllerConfig) -> None:
    server = make_server(config)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
