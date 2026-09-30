"""server.py: token and origin gate, apply flow (no Docker, no SearXNG)."""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
import settings as st  # noqa: E402


@pytest.fixture
def srv(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"server": {"secret_key": "k"}, "engines": []}))
    monkeypatch.setattr(server, "STORE", st.Store(tmp_path / "config.yaml", tmp_path / "out"))
    monkeypatch.setattr(server, "TOKEN", "t0k")
    monkeypatch.setattr(server, "engine_names", lambda: {"google", "bing"})
    restarts = []
    monkeypatch.setattr(server, "restart_searxng", lambda wait_s=60: restarts.append(1) or {"ok": True})
    monkeypatch.setattr(server, "status", lambda: {"ok": True})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", restarts, tmp_path
    httpd.shutdown()


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def test_health_is_open_the_rest_needs_the_token(srv):
    base, _, _ = srv
    assert call(base, "GET", "/health")[0] == 200
    assert call(base, "GET", "/api/status")[0] == 401
    assert call(base, "GET", "/api/status", headers={"X-Admin-Token": "wrong"})[0] == 401
    assert call(base, "GET", "/api/status", headers={"X-Admin-Token": "t0k"})[0] == 200


def test_foreign_origin_is_refused_even_with_the_token(srv):
    base, _, _ = srv
    code, _, _ = call(base, "GET", "/api/status", headers={"X-Admin-Token": "t0k", "Origin": "http://evil.example"})
    assert code == 403
    code, _, h = call(base, "GET", "/api/status", headers={"X-Admin-Token": "t0k", "Origin": "http://localhost:9762"})
    assert code == 200 and h.get("Access-Control-Allow-Origin") == "http://localhost:9762"


def test_apply_restarts_only_when_searxng_changes(srv):
    base, restarts, tmp = srv
    h = {"X-Admin-Token": "t0k"}
    code, res, _ = call(base, "PUT", "/api/settings", {"adapter": {"default_engines": {"general": ["google"]}}}, h)
    assert code == 200 and res["restarted"] is False and restarts == []
    code, res, _ = call(base, "PUT", "/api/settings", {"searxng": {"engines": {"bing": True}}}, h)
    assert code == 200 and res["restarted"] is True and restarts == [1]
    assert {"name": "bing", "disabled": False} in yaml.safe_load((tmp / "out/searxng/settings.yml").read_text())["engines"]
    code, res, _ = call(base, "PUT", "/api/settings", {"searxng": {"engines": {"nope": True}}}, h)
    assert code == 422 and "unknown engine" in res["errors"][0]
