"""Search settings the web page changed (search-admin), read on the fly.

search-admin renders SETTINGS_DIR/adapter/adapter.json; it is mounted
read-only here (SEARCH_SETTINGS_FILE), so the agents that share this
container cannot rewrite it. Read again whenever its mtime changes; a missing
or broken file means the built-in defaults.

- default_engines: category -> "engine,engine" used when a caller names none;
- reader_proxy: egress proxy of /extract and /media, over PROXY_URL;
- extractor: which extractor reads a fetched page (reader.EXTRACTORS).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PATH = Path(os.environ.get("SEARCH_SETTINGS_FILE", "/srv/search-settings/adapter.json"))
_cache: tuple[float, dict[str, Any]] = (-1.0, {})


def current(path: Path | None = None) -> dict[str, Any]:
    global _cache
    p = path or PATH
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    if mtime != _cache[0]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        _cache = (mtime, d if isinstance(d, dict) else {})
    return _cache[1]


def default_engines(category: str, path: Path | None = None) -> str | None:
    v = (current(path).get("default_engines") or {}).get(category)
    return v if isinstance(v, str) and v.strip() else None


def reader_proxy(path: Path | None = None) -> str | None:
    v = current(path).get("reader_proxy")
    return v if isinstance(v, str) and v.strip() else None


def extractor(path: Path | None = None) -> str | None:
    v = current(path).get("extractor")
    return v if v in ("auto", "trafilatura", "readability", "defuddle") else None
