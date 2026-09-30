"""search-admin: the web page's hands on SearXNG.

The only service with the Docker socket, and it uses it for one container
(SEARXNG_CONTAINER): read its state, restart it. It is not on the adapter's
network and every call needs the admin token, so a research agent (it has a
shell in the adapter container) can neither reach it by name nor call it
through a forwarded port: the token lives in this container's env or log and
in the viewer's browser, never in the adapter.

  GET  /health                     no token; for the compose healthcheck
  GET  /api/status                 container, SearXNG engines and errors, settings
  PUT  /api/settings               validate, save, render; restart SearXNG if its part changed
  POST /api/restart                restart SearXNG
  POST /api/probe {category}       one search per category: which engines did not answer
  POST /api/proxy-test {proxy}     reach the web through one proxy
"""
from __future__ import annotations

import hmac
import http.client
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import settings as st

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("search-admin")

DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
CONTAINER = os.environ.get("SEARXNG_CONTAINER", "searxng")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:9762").split(",") if o.strip()}
STORE = st.Store(Path(os.environ.get("BASE_CONFIG", "/srv/base/config.yaml")),
                 Path(os.environ.get("SETTINGS_DIR", "/srv/search-settings")))
ENV_SHOWN = ("SEARXNG_BASE_URL", "SEARXNG_SETTINGS_PATH", "BIND_ADDRESS", "SEARXNG_PORT", "GRANIAN_WORKERS")
_lock = threading.Lock()   # one apply or restart at a time


def admin_token() -> str:
    t = os.environ.get("SEARCH_ADMIN_TOKEN", "").strip()
    if t:
        return t
    t = secrets.token_urlsafe(24)
    log.warning("SEARCH_ADMIN_TOKEN is not set; this run's token (paste it into the Settings page): %s", t)
    return t


TOKEN = ""


# ---------- Docker (one container only) ----------

class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def docker(method: str, path: str, timeout: float = 30) -> tuple[int, Any]:
    conn = _UnixConn(DOCKER_SOCK, timeout)
    try:
        conn.request(method, f"/v1.41/containers/{urllib.parse.quote(CONTAINER)}{path}")
        r = conn.getresponse()
        body = r.read()
        try:
            return r.status, json.loads(body) if body else None
        except ValueError:
            return r.status, body.decode("utf-8", "replace")
    finally:
        conn.close()


def container_info() -> dict[str, Any]:
    try:
        code, d = docker("GET", "/json", timeout=5)
    except OSError as e:
        return {"error": f"docker socket: {type(e).__name__}"}
    if code != 200 or not isinstance(d, dict):
        return {"error": f"docker: {code}"}
    s, c, h = d.get("State") or {}, d.get("Config") or {}, d.get("HostConfig") or {}
    env = {}
    for kv in c.get("Env") or []:
        k, _, v = kv.partition("=")
        if k in ENV_SHOWN:
            env[k] = v
    ports = []
    for cport, binds in ((d.get("NetworkSettings") or {}).get("Ports") or {}).items():
        for b in binds or []:
            ports.append(f"{b.get('HostIp')}:{b.get('HostPort')} -> {cport}")
    return {
        "name": (d.get("Name") or "").lstrip("/"), "image": c.get("Image"), "image_id": (d.get("Image") or "")[:19],
        "created": d.get("Created"), "started_at": s.get("StartedAt"), "state": s.get("Status"),
        "health": (s.get("Health") or {}).get("Status"), "restart_count": d.get("RestartCount"),
        "restart_policy": (h.get("RestartPolicy") or {}).get("Name"),
        "mounts": [{"source": m.get("Source"), "destination": m.get("Destination"), "rw": m.get("RW")}
                   for m in d.get("Mounts") or []],
        "ports": ports, "env": env, "networks": sorted(((d.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
    }


def restart_searxng(wait_s: float = 60) -> dict[str, Any]:
    t0 = time.time()
    code, body = docker("POST", "/restart?t=5", timeout=30)
    if code not in (204, 200):
        return {"ok": False, "error": f"docker restart: {code} {str(body)[:200]}"}
    while time.time() - t0 < wait_s:
        time.sleep(1.5)
        try:
            searxng_get("/config", timeout=3)
            return {"ok": True, "seconds": round(time.time() - t0, 1)}
        except Exception:
            continue
    info = container_info()
    return {"ok": False, "error": f"SearXNG did not answer in {wait_s:.0f}s (state {info.get('state')})"}


# ---------- SearXNG ----------

def searxng_get(path: str, timeout: float = 10) -> Any:
    with urllib.request.urlopen(urllib.request.Request(SEARXNG_URL + path, headers={"X-Real-IP": "127.0.0.1"}),
                                timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def engines_view() -> dict[str, Any]:
    try:
        cfg = searxng_get("/config")
    except Exception as e:
        return {"reachable": False, "error": f"{type(e).__name__}: {e}"[:200], "engines": []}
    try:
        errs = searxng_get("/stats/errors")
    except Exception:
        errs = {}
    rows = []
    for e in cfg.get("engines") or []:
        er = errs.get(e.get("name")) or []
        rows.append({"name": e.get("name"), "categories": e.get("categories") or [], "enabled": bool(e.get("enabled")),
                     "shortcut": e.get("shortcut"), "timeout": e.get("timeout"),
                     "errors": [{"percentage": x.get("percentage"), "exception": x.get("exception_classname"),
                                 "message": (x.get("log_message") or "")[:160]} for x in er][:4]})
    return {"reachable": True, "version": cfg.get("version"), "safe_search": cfg.get("safe_search"),
            "limiter": cfg.get("limiter"), "engines": rows}


def engine_names() -> set[str]:
    try:
        return {e["name"] for e in searxng_get("/config", timeout=5).get("engines") or [] if e.get("name")}
    except Exception:
        return set()


def probe(category: str, query: str = "python release") -> dict[str, Any]:
    if category not in st.CATEGORIES:
        return {"error": "unknown category"}
    q = urllib.parse.urlencode({"q": query, "format": "json", "categories": category})
    t0 = time.time()
    try:
        d = searxng_get(f"/search?{q}", timeout=25)
    except Exception as e:
        return {"error": f"{type(e).__name__}"}
    answered: dict[str, int] = {}
    for r in d.get("results") or []:
        for name in r.get("engines") or [r.get("engine")]:
            if name:
                answered[name] = answered.get(name, 0) + 1
    return {"category": category, "seconds": round(time.time() - t0, 1), "results": len(d.get("results") or []),
            "answered": answered, "unresponsive": [{"engine": u[0], "reason": u[1]} for u in d.get("unresponsive_engines") or []]}


def proxy_test(url: str) -> dict[str, Any]:
    err = st.check_proxy(url)
    if err:
        return {"ok": False, "error": err}
    import httpx
    t0 = time.time()
    try:
        with httpx.Client(proxy=url, timeout=10, trust_env=False) as c:
            r = c.get("https://www.google.com/generate_204")
        return {"ok": r.status_code in (200, 204), "status": r.status_code, "seconds": round(time.time() - t0, 2)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


def status() -> dict[str, Any]:
    ov = STORE.overrides()
    base = STORE.base()
    rendered = st.render_searxng(base, ov)
    out = rendered.get("outgoing") or {}
    return {
        "container": container_info(),
        "searxng": engines_view(),
        "effective": {
            "limiter": (rendered.get("server") or {}).get("limiter"),
            "image_proxy": (rendered.get("server") or {}).get("image_proxy"),
            "safesearch": (rendered.get("search") or {}).get("safe_search"),
            "formats": (rendered.get("search") or {}).get("formats"),
            "request_timeout": out.get("request_timeout"), "max_request_timeout": out.get("max_request_timeout"),
            "proxies": [st.mask_proxy(u) for u in (out.get("proxies") or {}).get("all://", [])],
        },
        "overrides": st.masked(ov),
        "files": {"base": str(STORE.base_path), "searxng": str(STORE.searxng_path), "adapter": str(STORE.adapter_path)},
        "categories": list(st.CATEGORIES),
    }


def apply(body: Any) -> tuple[int, dict[str, Any]]:
    with _lock:
        stored = STORE.overrides()
        names = engine_names()
        ov, errors = st.normalize(body, stored, names)
        if errors:
            return 422, {"errors": errors}
        searxng_changed = ov["searxng"] != stored["searxng"]
        STORE.save(ov)
        result: dict[str, Any] = {"saved": True, "restarted": False}
        if searxng_changed or body.get("restart") is True:
            result["restart"] = restart_searxng()
            result["restarted"] = True
        log.info("settings saved (searxng changed: %s)", searxng_changed)
        return 200, result


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "search-admin"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")

    def _send(self, code: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin and origin not in ORIGINS:
            self._send(403, {"error": "origin not allowed"})
            return False
        if not hmac.compare_digest(self.headers.get("X-Admin-Token", "").encode(), TOKEN.encode()):
            self._send(401, {"error": "admin token required"})
            return False
        return True

    def _json(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if not self._allowed():
            return
        if self.path == "/api/status":
            return self._send(200, status())
        self._send(404, {"error": "not found"})

    def do_PUT(self) -> None:
        if not self._allowed():
            return
        if self.path != "/api/settings":
            return self._send(404, {"error": "not found"})
        try:
            body = self._json()
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        code, res = apply(body)
        self._send(code, res)

    def do_POST(self) -> None:
        if not self._allowed():
            return
        try:
            body = self._json()
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        if self.path == "/api/restart":
            with _lock:
                return self._send(200, restart_searxng())
        if self.path == "/api/probe":
            return self._send(200, probe(str(body.get("category") or "general")))
        if self.path == "/api/proxy-test":
            url = body.get("proxy") or ""
            url = st.keep_secret(url, STORE.overrides()["searxng"]["proxies"] +
                                 [STORE.overrides()["adapter"].get("reader_proxy") or ""])
            return self._send(200, proxy_test(url))
        self._send(404, {"error": "not found"})


def main() -> None:
    global TOKEN
    TOKEN = admin_token()
    STORE.render()   # SearXNG starts after this healthcheck passes: its settings must exist
    port = int(os.environ.get("PORT", "8011"))
    log.info("search-admin on :%d, container %s, origins %s", port, CONTAINER, ", ".join(sorted(ORIGINS)))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
