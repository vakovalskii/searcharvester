"""Search settings: what the web page may change, and the files it becomes.

Three layers, one direction:

- base: the repo owner's config.yaml (SearXNG settings plus the adapter
  section), never written here;
- overrides: SETTINGS_DIR/overrides.json, what the page changed;
- rendered: SETTINGS_DIR/searxng/settings.yml (SearXNG reads it at start,
  SEARXNG_SETTINGS_PATH) and SETTINGS_DIR/adapter/adapter.json (the adapter
  reads it on the fly). Both directories are mounted read-only into their
  containers, so nothing there can rewrite them.

Proxy URLs may carry credentials: they leave this module only masked, and a
masked value sent back unchanged keeps the stored one.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")
ENGINE_LIST_RE = re.compile(r"^[a-z0-9 ._-]{1,60}$")
CATEGORIES = ("general", "news", "images", "videos", "science", "it", "files", "social", "music", "map")
MASK = "***"
EXTRACTORS = ("auto", "trafilatura", "readability")  # simple_tavily_adapter/reader.py


def empty() -> dict[str, Any]:
    return {
        "searxng": {"engines": {}, "proxies": [], "request_timeout": None, "max_request_timeout": None},
        "adapter": {"default_engines": {}, "reader_proxy": None, "extractor": None},
    }


# ---------- proxies ----------

def mask_proxy(url: str | None) -> str | None:
    """scheme://user:***@host:port; no credentials, no change."""
    if not url:
        return url
    p = urlsplit(url)
    if p.password is None and p.username is None:
        return url
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    user = p.username or ""
    return urlunsplit((p.scheme, f"{user}:{MASK}@{host}", p.path, p.query, p.fragment))


def check_proxy(url: str) -> str | None:
    """Error text, or None when the URL is a usable proxy address."""
    try:
        p = urlsplit(url.strip())
        port = p.port
    except ValueError:
        return f"not a URL: {url!r}"
    if p.scheme not in PROXY_SCHEMES:
        return f"proxy scheme must be one of {', '.join(PROXY_SCHEMES)}: {mask_proxy(url)}"
    if not p.hostname:
        return f"proxy without a host: {mask_proxy(url)}"
    if port is None:
        return f"proxy without a port: {mask_proxy(url)}"
    if p.path not in ("", "/") or p.query or p.fragment:
        return f"proxy URL has a path or query: {mask_proxy(url)}"
    return None


def keep_secret(new: str | None, old_list: list[str]) -> str | None:
    """A masked value that matches a stored proxy's mask means 'unchanged'."""
    if not new or MASK not in new:
        return new
    for old in old_list:
        if mask_proxy(old) == new:
            return old
    return new   # a mask we never issued: check_proxy() fails on it later if bad


# ---------- overrides ----------

def normalize(body: Any, stored: dict[str, Any], engine_names: set[str]) -> tuple[dict[str, Any], list[str]]:
    """(overrides, errors) from a page's PUT body over the stored overrides."""
    errors: list[str] = []
    out = copy.deepcopy(stored) if stored else empty()
    if not isinstance(body, dict):
        return out, ["body must be an object"]
    sx = body.get("searxng") or {}
    ad = body.get("adapter") or {}
    if not isinstance(sx, dict) or not isinstance(ad, dict):
        return out, ["searxng and adapter must be objects"]
    old_proxies = list(stored.get("searxng", {}).get("proxies") or []) if stored else []

    if "engines" in sx:
        eng = sx["engines"]
        if not isinstance(eng, dict):
            errors.append("searxng.engines must map engine name to true/false")
        else:
            clean = {}
            for name, on in eng.items():
                if not isinstance(on, bool):
                    errors.append(f"engine {name!r}: enabled must be true or false")
                elif engine_names and name not in engine_names:
                    errors.append(f"unknown engine {name!r}")
                else:
                    clean[name] = on
            out["searxng"]["engines"] = clean

    if "proxies" in sx:
        lst = sx["proxies"]
        if not isinstance(lst, list) or len(lst) > 20:
            errors.append("searxng.proxies must be a list of at most 20 URLs")
        else:
            clean = []
            for u in lst:
                if not isinstance(u, str) or not u.strip():
                    continue
                u = keep_secret(u.strip(), old_proxies)
                err = check_proxy(u)
                errors.append(err) if err else clean.append(u)
            out["searxng"]["proxies"] = clean

    for key, lo, hi in (("request_timeout", 1.0, 60.0), ("max_request_timeout", 1.0, 120.0)):
        if key in sx:
            v = sx[key]
            if v is None:
                out["searxng"][key] = None
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
                out["searxng"][key] = float(v)
            else:
                errors.append(f"searxng.{key} must be a number from {lo:g} to {hi:g} seconds")
    rt, mrt = out["searxng"].get("request_timeout"), out["searxng"].get("max_request_timeout")
    if rt and mrt and mrt < rt:
        errors.append("max_request_timeout must not be below request_timeout")

    if "default_engines" in ad:
        de = ad["default_engines"]
        if not isinstance(de, dict):
            errors.append("adapter.default_engines must map category to a list of engines")
        else:
            clean = {}
            for cat, names in de.items():
                if cat not in CATEGORIES:
                    errors.append(f"unknown category {cat!r}")
                    continue
                if names in (None, [], ""):
                    continue   # the category's own engines
                if isinstance(names, str):
                    names = [n.strip() for n in names.split(",") if n.strip()]
                if not isinstance(names, list) or not all(isinstance(n, str) and ENGINE_LIST_RE.match(n) for n in names):
                    errors.append(f"default engines of {cat!r} must be engine names")
                    continue
                bad = [n for n in names if engine_names and n not in engine_names]
                if bad:
                    errors.append(f"unknown engine in {cat!r}: {', '.join(bad)}")
                    continue
                clean[cat] = names[:12]
            out["adapter"]["default_engines"] = clean

    if "reader_proxy" in ad:
        v = ad["reader_proxy"]
        if not v:
            out["adapter"]["reader_proxy"] = None
        elif isinstance(v, str):
            old = stored.get("adapter", {}).get("reader_proxy") if stored else None
            v = keep_secret(v.strip(), [old] if old else [])
            err = check_proxy(v)
            if err:
                errors.append(err)
            else:
                out["adapter"]["reader_proxy"] = v
        else:
            errors.append("adapter.reader_proxy must be a URL or empty")

    if "extractor" in ad:
        v = ad["extractor"]
        if v in (None, ""):
            out["adapter"]["extractor"] = None
        elif v in EXTRACTORS:
            out["adapter"]["extractor"] = v
        else:
            errors.append(f"adapter.extractor must be one of {', '.join(EXTRACTORS)}")
    return out, errors


def masked(ov: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(ov)
    out["searxng"]["proxies"] = [mask_proxy(u) for u in out["searxng"].get("proxies") or []]
    out["adapter"]["reader_proxy"] = mask_proxy(out["adapter"].get("reader_proxy"))
    return out


# ---------- rendering ----------

def render_searxng(base: dict[str, Any], ov: dict[str, Any]) -> dict[str, Any]:
    """SearXNG settings: the base without the adapter section, plus overrides."""
    s = copy.deepcopy({k: v for k, v in (base or {}).items() if k != "adapter"})
    s.setdefault("use_default_settings", True)
    sx = ov.get("searxng") or {}
    outgoing = dict(s.get("outgoing") or {})
    if sx.get("request_timeout"):
        outgoing["request_timeout"] = sx["request_timeout"]
    if sx.get("max_request_timeout"):
        outgoing["max_request_timeout"] = sx["max_request_timeout"]
    if sx.get("proxies"):
        # One pool for every engine; SearXNG rotates through the list.
        outgoing["proxies"] = {"all://": list(sx["proxies"])}
    if outgoing:
        s["outgoing"] = outgoing
    engines = [dict(e) for e in (s.get("engines") or []) if isinstance(e, dict) and e.get("name")]
    by_name = {e["name"]: e for e in engines}
    for name, on in sorted((sx.get("engines") or {}).items()):
        if name in by_name:
            by_name[name]["disabled"] = not on
        else:
            engines.append({"name": name, "disabled": not on})
    if engines:
        s["engines"] = engines
    return s


def render_adapter(ov: dict[str, Any]) -> dict[str, Any]:
    ad = ov.get("adapter") or {}
    return {"default_engines": {k: ",".join(v) for k, v in (ad.get("default_engines") or {}).items()},
            "reader_proxy": ad.get("reader_proxy"),
            "extractor": ad.get("extractor")}


# ---------- files ----------

class Store:
    def __init__(self, base_path: Path, settings_dir: Path):
        self.base_path = Path(base_path)
        self.dir = Path(settings_dir)

    @property
    def overrides_path(self) -> Path:
        return self.dir / "overrides.json"

    @property
    def searxng_path(self) -> Path:
        return self.dir / "searxng" / "settings.yml"

    @property
    def adapter_path(self) -> Path:
        return self.dir / "adapter" / "adapter.json"

    def base(self) -> dict[str, Any]:
        return yaml.safe_load(self.base_path.read_text(encoding="utf-8")) or {}

    def overrides(self) -> dict[str, Any]:
        try:
            d = json.loads(self.overrides_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return empty()
        e = empty()
        for k in e:
            if isinstance(d.get(k), dict):
                e[k].update(d[k])
        return e

    def save(self, ov: dict[str, Any]) -> None:
        _atomic(self.overrides_path, json.dumps(ov, ensure_ascii=False, indent=2), mode=0o600)
        self.render(ov)

    def render(self, ov: dict[str, Any] | None = None) -> None:
        ov = ov if ov is not None else self.overrides()
        # SearXNG runs as its own user: the file must be readable, the secret key
        # in it stays inside this directory (gitignored, mounted read-only).
        _atomic(self.searxng_path, yaml.safe_dump(render_searxng(self.base(), ov), allow_unicode=True, sort_keys=False),
                mode=0o644)
        _atomic(self.adapter_path, json.dumps(render_adapter(ov), ensure_ascii=False, indent=2), mode=0o644)


def _atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
