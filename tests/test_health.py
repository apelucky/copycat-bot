"""HTTP health endpoint used to satisfy Render's free web-service plan."""
from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from copycat.bot.telegram_app import run
from copycat.config import AppConfig
from copycat.http_health import HealthServer, current, start_from_env


def _get(url: str, method: str = "GET") -> tuple[int, bytes]:
    req = Request(url, method=method)
    try:
        with urlopen(req, timeout=3) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


def test_health_and_root_return_200():
    server = HealthServer(0, lambda: {"ok": True, "service": "copycat", "mode": "paper", "db": "sqlite", "db_ok": True})
    server.start()
    try:
        for path in ("/", "/health", "/health/", "/health?x=1"):
            status, body = _get(f"http://127.0.0.1:{server.port}{path}")
            assert status == 200
            payload = json.loads(body)
            assert payload["ok"] is True and payload["mode"] == "paper" and payload["db"] == "sqlite"
        status, body = _get(f"http://127.0.0.1:{server.port}/health", method="HEAD")
        assert status == 200 and body == b""
        status, body = _get(f"http://127.0.0.1:{server.port}/nope")
        assert status == 404
        assert json.loads(body)["ok"] is False
    finally:
        server.stop()


def test_missing_port_does_not_listen(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert start_from_env(lambda: {"ok": True}) is None


def test_run_answers_health_while_polling(monkeypatch, tmp_path):
    """The real entry path binds $PORT before Telegram polling, without calling Telegram."""
    monkeypatch.setenv("PORT", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AAFakeTokenForHealthOnly")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "health.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    started = threading.Event()
    release = threading.Event()

    class FakeApp:
        def add_handler(self, *args, **kwargs):
            return None

        def run_polling(self, **kwargs):
            started.set()
            release.wait(5)

    class FakeBuilder:
        def token(self, _token):
            return self

        def post_init(self, _fn):
            return self

        def post_shutdown(self, _fn):
            return self

        def build(self):
            return FakeApp()

    monkeypatch.setattr("copycat.bot.telegram_app.Application.builder", lambda: FakeBuilder())
    errors: list[BaseException] = []

    def go() -> None:
        try:
            run(AppConfig.from_env())
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
            started.set()

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    assert started.wait(15), errors
    assert not errors
    server = current()
    assert server is not None and server.port != 0
    status, body = _get(f"http://127.0.0.1:{server.port}/health")
    payload = json.loads(body)
    assert status == 200
    assert payload["ok"] is True
    assert payload["service"] == "copycat"
    assert payload["mode"] == "paper"
    assert payload["db"] == "sqlite"
    assert payload["db_ok"] is True
    status, root = _get(f"http://127.0.0.1:{server.port}/")
    assert status == 200 and json.loads(root)["ok"] is True
    release.set()
    thread.join(5)
    assert not errors
    assert not thread.is_alive()
