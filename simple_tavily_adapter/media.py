"""Images and videos of a research job: what search handed out, and /media.

Stage D of docs/ui-and-visualization.md, fed by /search. Every image URL,
thumbnail and video an agent of a job got from /search is written to the job's
ledger STATE_DIR/<id>/media.jsonl by the adapter (the agent cannot write there).
The ledger is the only allow list of GET /media: a report can show a picture
only if search really returned it to this job, and the browser never fetches
from a foreign site, the adapter does.

The fetch is the reader's (reader.py): http(s) only, every hop checked against
private addresses and pinned to the checked IP, hand-followed redirects, no
Referer and no cookies. Only png, jpeg, gif, webp and avif pass, by the first
bytes (not by the header), at most MAX_BYTES; the file is cached under a name
the server builds from sha256(src).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx

import reader

logger = logging.getLogger(__name__)

LEDGER = "media.jsonl"
CACHE_DIR = "media"
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 10.0
MAX_REDIRECTS = 3
MAX_PER_JOB = 2000            # ledger rows: a runaway agent must not fill the disk
JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")

TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "avif": "image/avif"}


class MediaError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def sniff(head: bytes) -> Optional[str]:
    """Image type by signature; None for anything else (SVG, HTML, video, ...)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "avif"
    return None


def _http_url(u: Any) -> Optional[str]:
    if not isinstance(u, str):
        return None
    u = u.strip()
    if u.startswith("//"):
        u = "https:" + u
    try:
        p = urlparse(u)
    except ValueError:
        return None
    return u if p.scheme in ("http", "https") and p.hostname and len(u) <= 2048 else None


_YT_ID = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_thumb(page: str) -> Optional[str]:
    """A short, stable preview of a YouTube video. Engines hand out long opaque
    proxy URLs (imgs.search.brave.com/<base64>) that a model copies with typos."""
    m = _YT_ID.search(page or "")
    return f"https://i.ytimg.com/vi/{m.group(1)}/hqdefault.jpg" if m else None


def _loose(u: str) -> str:
    """What survives a model's retyping of a URL: no slashes, no whitespace."""
    return re.sub(r"[/\s]+", "", u)


def items_from_results(results: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    """Ledger rows from SearXNG results: the image itself, its thumbnail, a video
    with its thumbnail. `page` is where it lives, for the link next to it."""
    out: list[dict[str, Any]] = []
    for r in results:
        page = _http_url(r.get("url"))
        if not page:
            continue
        title = str(r.get("title") or "")[:300]
        img = _http_url(r.get("img_src"))
        thumb = _http_url(r.get("thumbnail_src")) or _http_url(r.get("thumbnail"))
        is_video = category == "videos" or r.get("template") == "videos.html"
        if is_video:
            yt = youtube_thumb(page)
            row = {"kind": "video", "src": page, "thumb": yt or thumb, "page": page, "title": title,
                   "duration": _duration(r.get("length"))}
            if yt and thumb and thumb != yt:
                row["alt_thumb"] = thumb   # the engine's own preview stays allowed
            out.append(row)
        elif img or thumb:
            out.append({"kind": "image", "src": img or thumb, "thumb": thumb, "page": page, "title": title,
                        "size": str(r.get("resolution") or "")[:40] or None})
    return out


def _duration(v: Any) -> Optional[str]:
    """SearXNG gives "525.0" (seconds) or "01:58"; the UI wants m:ss."""
    if v in (None, ""):
        return None
    s = str(v).strip()
    try:
        sec = int(float(s))
    except ValueError:
        return s[:12] if re.fullmatch(r"\d{1,2}(:\d{2}){1,2}", s) else None
    h, rem = divmod(sec, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


class Ledger:
    """Per-job list of media search handed out; also the /media allow list."""

    def __init__(self, state_dir: Path):
        self._dir = Path(state_dir)
        self._cache: dict[str, tuple[float, dict[str, str], list[dict]]] = {}

    def _path(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id or ""):
            raise MediaError(400, "bad job id")
        return self._dir / job_id / LEDGER

    def record(self, job_id: str, items: list[dict[str, Any]], query: str = "") -> int:
        if not items:
            return 0
        p = self._path(job_id)
        have = self._load(job_id)[1]
        rows = [dict(i, query=query[:300], ts=time.time()) for i in items
                if i["src"] not in have or (i.get("thumb") and i["thumb"] not in have)]
        if not rows or len(have) >= MAX_PER_JOB:
            return 0
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        self._cache.pop(job_id, None)
        return len(rows)

    def _load(self, job_id: str) -> tuple[float, dict[str, str], list[dict]]:
        """(mtime, allowed: exact or loose form -> the URL as search gave it, rows)."""
        p = self._path(job_id)
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            return 0.0, {}, []
        hit = self._cache.get(job_id)
        if hit and hit[0] == mtime:
            return hit
        rows: list[dict] = []
        allowed: dict[str, str] = {}
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and isinstance(d.get("src"), str):
                rows.append(d)
                for k in ("src", "thumb", "alt_thumb"):
                    u = d.get(k)
                    if isinstance(u, str):
                        allowed.setdefault(u, u)
                        allowed.setdefault("~" + _loose(u), u)
        self._cache[job_id] = (mtime, allowed, rows)
        return self._cache[job_id]

    def resolve(self, job_id: str, src: str) -> Optional[str]:
        """The ledger URL a requested src stands for, or None. A slash or a space
        lost in retyping still finds its entry; what gets fetched is always the
        ledger's own URL, never the requested one."""
        allowed = self._load(job_id)[1]
        return allowed.get(src) or allowed.get("~" + _loose(src))

    def allowed(self, job_id: str, src: str) -> bool:
        return self.resolve(job_id, src) is not None

    def items(self, job_id: str) -> list[dict[str, Any]]:
        """Unique by src, first seen first."""
        seen, out = set(), []
        for r in self._load(job_id)[2]:
            if r["src"] in seen:
                continue
            seen.add(r["src"])
            out.append({k: r.get(k) for k in ("kind", "src", "thumb", "page", "title", "duration", "size", "query")})
        return out

    def cache_file(self, job_id: str, src: str, ext: str) -> Path:
        d = (self._dir / job_id / CACHE_DIR).resolve()
        f = (d / f"{hashlib.sha256(src.encode('utf-8')).hexdigest()}.{ext}").resolve()
        if f.parent != d:
            raise MediaError(400, "bad cache path")
        return f

    def cached(self, job_id: str, src: str) -> Optional[tuple[Path, str]]:
        for ext, ctype in TYPES.items():
            f = self.cache_file(job_id, src, ext)
            if f.exists():
                return f, ctype
        return None


@dataclass
class Image:
    body: bytes
    ext: str


async def fetch_image(src: str, *, proxy: Optional[str] = None) -> Image:
    """GET an image the safe way (see the module doc); MediaError on refusal."""
    current = src
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_S, connect=5.0), follow_redirects=False, trust_env=False, proxy=proxy,
            headers={"User-Agent": reader.UA, "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9"},
        ) as client:
            async def run() -> Image:
                nonlocal current
                for _ in range(MAX_REDIRECTS + 1):
                    if not await reader.is_safe_url(current):
                        raise MediaError(400, "address not allowed")
                    kind, addrs = await reader.resolve_addrs(urlparse(current).hostname or "")
                    if kind != "global" or not addrs:
                        raise MediaError(400 if kind == "private" else 502, "address not allowed" if kind == "private" else "host not resolved")
                    target, host_header, ext = (current, {}, {}) if proxy else reader._pinned(current, addrs[0])
                    headers = {**host_header, "User-Agent": reader.user_agent_for(current)}
                    async with client.stream("GET", target, headers=headers, extensions=ext) as r:
                        if r.is_redirect:
                            loc = r.headers.get("location")
                            if not loc:
                                raise MediaError(502, "redirect without location")
                            current = urljoin(current, loc)
                            continue
                        if r.status_code != 200:
                            raise MediaError(502, f"upstream {r.status_code}")
                        declared = int(r.headers.get("content-length") or 0)
                        if declared > MAX_BYTES:
                            raise MediaError(413, "image too large")
                        body = bytearray()
                        async for chunk in r.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_BYTES:
                                raise MediaError(413, "image too large")
                        kind_ = sniff(bytes(body[:16]))
                        if kind_ is None:
                            raise MediaError(415, "not a supported image")
                        return Image(bytes(body), kind_)
                raise MediaError(502, "too many redirects")
            return await asyncio.wait_for(run(), timeout=TIMEOUT_S * 2)
    except MediaError:
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise MediaError(504, "image fetch timed out")
    except Exception as e:
        raise MediaError(502, f"image fetch failed: {type(e).__name__}")
