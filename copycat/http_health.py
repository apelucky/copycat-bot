"""Tiny HTTP server for Render's free web-service plan.

Render only keeps free *web* services, and only while something accepts HTTP on $PORT.
python-telegram-bot's polling loop owns the main thread, so this server runs beside it
on a daemon thread. Local runs that do not set PORT do not open a port.

`/` and `/health` return 200 with a short JSON status. No balances, tokens, or secrets.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

StatusFn = Callable[[], dict[str, Any]]
_server: "HealthServer | None" = None


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._respond(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond(include_body=False)

    def _respond(self, include_body: bool) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path not in ("/", "/health"):
            body = b'{"ok":false,"error":"not found"}'
            self._send(404, body, include_body)
            return
        try:
            payload = self.server.status()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.exception("health status failed")
            payload = {"ok": True, "service": "copycat", "mode": "paper", "db_ok": False}
        body = json.dumps(payload, separators=(",", ":")).encode()
        self._send(200, body, include_body)

    def _send(self, code: int, body: bytes, include_body: bool) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("http " + fmt, *args)


class HealthServer:
    def __init__(self, port: int, status: StatusFn):
        self.httpd = _Server(("0.0.0.0", port), _Handler)
        self.httpd.status = status  # type: ignore[attr-defined]
        self.port = int(self.httpd.server_address[1])
        self._thread: threading.Thread | None = None

    def start(self) -> "HealthServer":
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, name="copycat-health", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        global _server
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if _server is self:
            _server = None


def health_status(cfg: Any, engine: Any) -> dict[str, Any]:
    from . import __version__

    db = "postgres" if getattr(cfg, "database_url", None) else "sqlite"
    db_ok = False
    try:
        db_ok = bool(engine.repo.ping())
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"ok": True, "service": "copycat", "version": __version__, "mode": "paper", "db": db, "db_ok": db_ok}


def start_from_env(status: StatusFn) -> HealthServer | None:
    """Listen on $PORT when it is set. Return None for a normal local run."""
    global _server
    raw = os.getenv("PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"PORT must be a number, got {raw!r}") from None
    try:
        server = HealthServer(port, status).start()
    except OSError as e:
        raise SystemExit(f"Could not listen on PORT {port}: {e}") from e
    _server = server
    log.info("health server on 0.0.0.0:%s (/ and /health)", server.port)
    return server


def current() -> HealthServer | None:
    return _server
