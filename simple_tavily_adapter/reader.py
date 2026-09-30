"""
Page reader: how /extract opens a page.

Cascade (ported from neuraldeep search-api, 2026-09-29):
  0. safety: http(s) only, every resolved address must be global, redirects are
     followed by hand and re-checked, the connection goes to the address that was
     checked (no DNS rebinding). The agent has a shell, so /extract must not become
     a way into the docker network (redis, searxng, cloud metadata);
  1. fast: plain GET + trafilatura, free and ~0.5 s. A quality gate decides whether
     the text is good enough: hard reject, hard accept, or ask a small LLM judge;
  2. reader: a remote reader (neuraldeep /v1/search/read, paid) that passes most
     anti-bot and client-rendered pages;
  3. browser: optional playwright service (PLAYWRIGHT_URL).
Foreign sites that refuse a direct request are retried through PROXY_URL, if set.
Russian sites never go through it: they block foreign IPs.

Every call is logged (sqlite, see read_log.py) so the gate can be tuned on data.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import atexit
import logging
import os
import re
import select
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

try:  # optional second extractor, see extract_readability()
    import markdownify as _markdownify
    from readability import Document as _ReadabilityDoc
except ImportError:  # pragma: no cover - the image always has them
    _markdownify = _ReadabilityDoc = None

logger = logging.getLogger(__name__)

# ── Safety ─────────────────────────────────────────────────────────────────

# Cheap first filter, kept for readable 400s. The real check is resolve_is_global().
BLOCKED_PATTERNS = [
    "169.254.169.254", "localhost", "127.", "10.", "192.168.",
    "172.16.", "172.17.", "172.18.", "172.19.", "172.2", "172.30.", "172.31.",
]


async def resolve_addrs(host: str) -> tuple[str, list[str]]:
    """('global' | 'private' | 'unresolved', addresses). 'global' means every
    address is public, 'private' that any is internal, 'unresolved' that our DNS
    does not know the name (we cannot reach it either, so it is not an SSRF risk,
    but the fast path and the browser cannot open it)."""
    if not host:
        return "private", []
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return ("global" if ip.is_global else "private"), [str(ip)]
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except Exception:
        return "unresolved", []
    if not infos:
        return "unresolved", []
    addrs = []
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return "private", []
        except ValueError:
            return "private", []
        addrs.append(info[4][0])
    # IPv4 first: the egress proxy and some hosts have no IPv6 route.
    return "global", sorted(dict.fromkeys(addrs), key=lambda a: ":" in a)


async def resolve_host(host: str) -> str:
    return (await resolve_addrs(host))[0]


async def resolve_is_global(host: str) -> bool:
    return await resolve_host(host) == "global"


async def is_safe_url(url: str) -> bool:
    """False only for URLs that point into a private network or are not http(s).
    Hosts our DNS cannot resolve pass: the external reader may still open them
    (pmc.ncbi.nlm.nih.gov did not resolve from TW-2 on 2026-09-28)."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if any(pattern in url.lower() for pattern in BLOCKED_PATTERNS):
        return False
    return await resolve_host(p.hostname) != "private"


# ── Proxy routing ──────────────────────────────────────────────────────────

_DOMESTIC_SUFFIXES = (".ru", ".xn--p1ai", ".su", ".by", ".kz", ".рф", ".moscow", ".xn--80adxhks")
# Statuses that usually mean "not from your IP" rather than "no such page".
_REFUSAL_STATUSES = {401, 403, 429, 451, 498}


def is_domestic(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host.endswith(_DOMESTIC_SUFFIXES)


def should_retry_via_proxy(url: str, status: Optional[int], error: Optional[str], proxy_configured: bool) -> bool:
    """Retry through the egress proxy only for foreign hosts that refused us."""
    if not proxy_configured or is_domestic(url):
        return False
    if error:
        return True
    return status in _REFUSAL_STATUSES


# ── Fetch ──────────────────────────────────────────────────────────────────

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
# Wikimedia answers 403 to any non-browser client that claims to be Chrome
# (robot policy https://w.wiki/4wJS) and lets an honest bot with a contact in.
BOT_UA = "SearcharvesterBot/2.2 (+https://github.com/vakovalskii/searcharvester)"
HONEST_UA_DOMAINS = ("wikipedia.org", "wikimedia.org", "wiktionary.org", "wikidata.org",
                     "wikibooks.org", "wikisource.org", "wikiquote.org", "wikivoyage.org")
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 3


def _pinned(url: str, ip: str) -> tuple[str, dict, dict]:
    """The same request aimed at an address we already checked.

    Connecting by name would resolve it again, and a name with a zero TTL can
    answer a public address to the check and an internal one to the connect (DNS
    rebinding). So the URL gets the checked IP, the Host header keeps the name and
    TLS still sends and verifies the name through sni_hostname.

    Direct requests only: httpcore ignores sni_hostname inside a proxy tunnel and
    verifies the certificate against the IP. Through the proxy the name is resolved
    by the proxy, which denies private and link-local destinations itself (3proxy
    deny list on the FI box, 28.09.26).
    """
    p = urlparse(url)
    netloc_ip = f"[{ip}]" if ":" in ip else ip
    if p.port:
        netloc_ip += f":{p.port}"
    host_header = p.hostname + (f":{p.port}" if p.port else "")
    extensions = {"sni_hostname": p.hostname} if p.scheme == "https" else {}
    return p._replace(netloc=netloc_ip).geturl(), {"Host": host_header}, extensions


@dataclass
class Fetched:
    status: Optional[int] = None
    html: str = ""
    final_url: str = ""
    error: Optional[str] = None


def user_agent_for(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return BOT_UA if any(host == d or host.endswith("." + d) for d in HONEST_UA_DOMAINS) else UA


async def fetch_html(url: str, proxy: Optional[str] = None) -> Fetched:
    """GET with a byte cap and hand-followed redirects, each hop checked for SSRF."""
    current = url
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            follow_redirects=False, trust_env=False, proxy=proxy,
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8",
                     "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                if not await is_safe_url(current):
                    return Fetched(error="unsafe_url", final_url=current)
                kind, addrs = await resolve_addrs(urlparse(current).hostname or "")
                if kind == "unresolved":
                    return Fetched(error="dns_unresolved", final_url=current)
                if kind != "global" or not addrs:
                    return Fetched(error="unsafe_url", final_url=current)
                target, host_header, extensions = (current, {}, {}) if proxy else _pinned(current, addrs[0])
                hop_headers = {**host_header, "User-Agent": user_agent_for(current)}
                async with client.stream("GET", target, headers=hop_headers, extensions=extensions) as r:
                    if r.is_redirect:
                        location = r.headers.get("location")
                        if not location:
                            return Fetched(status=r.status_code, final_url=current)
                        current = urljoin(current, location)
                        continue
                    if r.status_code != 200:
                        return Fetched(status=r.status_code, final_url=current)
                    ctype = r.headers.get("content-type", "")
                    if "html" not in ctype and "xml" not in ctype:
                        return Fetched(status=r.status_code, final_url=current, error="not_html")
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            break
                    encoding = r.encoding or "utf-8"
                    return Fetched(status=200, html=body.decode(encoding, errors="replace"), final_url=current)
            return Fetched(error="too_many_redirects", final_url=current)
    except httpx.TimeoutException:
        return Fetched(error="timeout", final_url=current)
    except Exception as e:
        return Fetched(error=type(e).__name__, final_url=current)


# ── Extraction and quality signals ─────────────────────────────────────────

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SCRIPT_RE = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<noscript\b.*?</noscript>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_EMPTY_ROOT_RE = re.compile(
    r"<div[^>]+id=[\"'](root|app|__next|__nuxt|svelte)[\"'][^>]*>\s*</div>|<app-root[^>]*>\s*</app-root>", re.I)
_ANTIBOT_RE = re.compile(
    r"checking your browser|just a moment|attention required|почти готово|проверяем ваш браузер|"
    r"access denied|доступ запрещ|are you a robot|вы не робот|captcha|ddos-guard|enable javascript", re.I)
_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]+\)")


EXTRACTORS = ("auto", "trafilatura", "readability", "defuddle")
DEFAULT_EXTRACTOR = "auto"
AUTO_EXTRACTORS = ("trafilatura", "readability", "defuddle")


def extract(html: str, url: str, return_format: str = "markdown") -> tuple[str, str]:
    """(title, text). Text is empty when trafilatura finds nothing."""
    m = _TITLE_RE.search(html)
    title = re.sub(r"\s+", " ", m.group(1)).strip()[:300] if m else ""
    text = trafilatura.extract(
        html, url=url, output_format="markdown" if return_format == "markdown" else "txt",
        include_tables=True, include_comments=False, favor_recall=True,
    ) or ""
    return title, text


def extract_readability(html: str, url: str, return_format: str = "markdown") -> tuple[str, str]:
    """(title, text) by readability-lxml (Mozilla Readability port), empty when it fails.

    Benchmark on the read log (2026-09-30): alone it passes the gate less often than
    trafilatura on pages trafilatura already reads (19 vs 23 of 30), but on pages
    trafilatura failed it pulls some through; hence "auto" runs both.
    """
    if _ReadabilityDoc is None:
        return "", ""
    try:
        doc = _ReadabilityDoc(html, url=url)
        body = doc.summary(html_partial=True)
        title = re.sub(r"\s+", " ", doc.short_title() or "").strip()[:300]
    except Exception:  # noqa: BLE001 - broken markup is a miss, not an error
        return "", ""
    if return_format == "markdown":
        text = _markdownify.markdownify(body, heading_style="ATX", strip=["img"]) or ""
    else:
        text = _TAG_RE.sub(" ", body)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


DEFUDDLE_WORKER = os.environ.get("DEFUDDLE_WORKER", "/opt/defuddle/defuddle_worker.mjs")
DEFUDDLE_TIMEOUT_S = 15.0


class _DefuddleWorker:
    """One long-lived `node defuddle_worker.mjs`, one JSON line each way per page.

    Calls are serialized (a worker handles one page at a time). A timeout, a crash or
    garbage on stdout kills the process; the next call starts a fresh one.
    """

    def __init__(self, script: str):
        self.script = script
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        atexit.register(self._kill)

    def _kill(self) -> None:
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
        self.proc = None

    def call(self, html: str, url: str, markdown: bool) -> dict | None:
        if not os.path.exists(self.script):
            return None
        with self.lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self.proc = subprocess.Popen(
                        ["node", self.script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
                self.proc.stdin.write(json.dumps({"html": html, "url": url, "markdown": markdown}) + "\n")
                self.proc.stdin.flush()
                ready, _, _ = select.select([self.proc.stdout], [], [], DEFUDDLE_TIMEOUT_S)
                line = self.proc.stdout.readline() if ready else ""
                if not line:
                    raise TimeoutError("defuddle worker: no answer")
                return json.loads(line)
            except Exception as e:  # noqa: BLE001 - a stuck page must not stick the worker
                logger.warning("defuddle worker reset: %s", e)
                self._kill()
                return None


_defuddle = _DefuddleWorker(DEFUDDLE_WORKER)


def extract_defuddle(html: str, url: str, return_format: str = "markdown") -> tuple[str, str]:
    """(title, text) by Defuddle (Obsidian Web Clipper's extractor, JS), empty when it fails."""
    if not html:
        return "", ""
    r = _defuddle.call(html, url, return_format == "markdown")
    if not r or r.get("error"):
        return "", ""
    text = r.get("content") or ""
    if return_format != "markdown":
        text = _TAG_RE.sub(" ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return re.sub(r"\s+", " ", r.get("title") or "").strip()[:300], text


_EXTRACTOR_FNS = {"trafilatura": "extract", "readability": "extract_readability", "defuddle": "extract_defuddle"}


_RANK = {"ok": 2, "judge": 1, "reject": 0}
_MD_LINK_TARGET_RE = re.compile(r"\[([^\]]*)\]\([^)]+\)")


def _reading_len(text: str) -> int:
    """Length a reader sees: [label](url) counts as label. readability keeps inline links
    and trafilatura mostly drops them, so raw length favoured readability for URLs alone
    (Wikipedia: 51k with 346 links vs 37k with none, same article)."""
    return len(_MD_LINK_TARGET_RE.sub(r"\1", text))


def extract_best(html: str, url: str, return_format: str, extractor: str):
    """[(name, title, text, sig, decision, reason)] best first, by gate verdict then length.

    trafilatura / readability give one candidate; auto gives both, so the longer text
    (by _reading_len, link targets excluded) wins only among candidates the gate does
    not reject.
    """
    names = AUTO_EXTRACTORS if extractor == "auto" else (extractor,)
    out = []
    for name in names:
        fn = globals()[_EXTRACTOR_FNS[name]]   # looked up per call: tests patch the module
        title, text = fn(html, url, return_format)
        if not title:
            m = _TITLE_RE.search(html)
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:300] if m else ""
        sig = signals(html, text, title)
        decision, reason = gate(sig)
        out.append((name, title, text, sig, decision, reason))
    out.sort(key=lambda c: (_RANK[c[4]], _reading_len(c[2])), reverse=True)
    return out


def signals(html: str, text: str, title: str) -> dict:
    visible = _TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", html))
    visible_len = len(re.sub(r"\s+", " ", visible).strip())
    links = len(_MD_LINK_RE.findall(text))
    return {
        "html_len": len(html),
        "text_len": len(text),
        "visible_ratio": round(visible_len / max(len(html), 1), 4),
        "scripts": len(re.findall(r"<script\b", html, re.I)),
        "empty_root": bool(_EMPTY_ROOT_RE.search(html)),
        "antibot": bool(_ANTIBOT_RE.search(title) or _ANTIBOT_RE.search(text[:2000])),
        "links_per_1k": round(links * 1000 / max(len(text), 1), 2),
    }


# Thresholds live here so the calibration run can report against them.
# Reader/browser answers shorter than this are stubs, not content (WB returned 17 chars).
MIN_PROVIDER_TEXT = 100
MIN_TEXT = 300
HARD_OK_TEXT = 6000  # 4000 let a 4k intro of a 24k article through (calibration 2026-09-28)
HARD_OK_MAX_LINKS_PER_1K = 6.0
SPA_VISIBLE_RATIO = 0.05


def gate(sig: dict) -> tuple[str, str]:
    """('reject'|'ok'|'judge', reason)."""
    if sig["antibot"]:
        return "reject", "antibot"
    if sig["text_len"] < MIN_TEXT:
        return "reject", "too_short"
    if sig["empty_root"] and sig["text_len"] < HARD_OK_TEXT:
        return "reject", "spa_shell"
    if sig["visible_ratio"] < SPA_VISIBLE_RATIO and sig["scripts"] > 20 and sig["text_len"] < HARD_OK_TEXT:
        return "reject", "spa_shell"
    if sig["text_len"] >= HARD_OK_TEXT and sig["links_per_1k"] <= HARD_OK_MAX_LINKS_PER_1K:
        return "ok", "long_clean_text"
    return "judge", "gray_zone"


# ── LLM judge ──────────────────────────────────────────────────────────────

JUDGE_PROMPT = (
    "You check whether text extracted from a web page is usable as the page content. "
    "Answer with JSON only: {\"ok\": true|false, \"reason\": one of "
    "\"article\", \"listing_ok\", \"listing_cut\", \"spa_shell\", \"antibot\", \"paywall\", "
    "\"cookie_wall\", \"error_page\", \"other\"}. "
    "ok=true when the text carries the main content a reader would expect at this URL "
    "(an article, docs, a product card, or a readable list of items for a feed or home page). "
    "ok=false when it is a stub: loading shell, captcha or bot check, login or cookie wall, "
    "paywall teaser, error page, or only navigation and a couple of lines while the page "
    "obviously holds much more. Also ok=false when the text reads like just the intro, "
    "the first section or a fragment of a longer article, list or reference page."
)


async def judge(url: str, title: str, text: str, sig: dict, settings) -> tuple[Optional[bool], str, int]:
    """(ok, reason, ms). ok=None when the judge is unavailable or answered garbage."""
    if not getattr(settings, "judge_llm_api_key", ""):
        return None, "judge_unconfigured", 0
    excerpt = text[:1500] + ("\n…\n" + text[-300:] if len(text) > 1800 else "")
    user = (f"URL: {url}\nTitle: {title}\nStats: {json.dumps(sig)}\n\n"
            f"Extracted text ({len(text)} chars, excerpt):\n{excerpt}")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=settings.judge_timeout_s, trust_env=False) as client:
            r = await client.post(
                f"{settings.judge_llm_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.judge_llm_api_key}"},
                json={"model": settings.judge_llm_model, "temperature": 0, "max_tokens": 60,
                      "messages": [{"role": "system", "content": JUDGE_PROMPT},
                                   {"role": "user", "content": user}]},
            )
        ms = int((time.monotonic() - t0) * 1000)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", raw, re.S)
        verdict = json.loads(m.group(0)) if m else {}
        ok = verdict.get("ok")
        if not isinstance(ok, bool):
            return None, "judge_garbage", ms
        return ok, str(verdict.get("reason", "other"))[:32], ms
    except Exception as e:
        return None, f"judge_error:{type(e).__name__}"[:32], int((time.monotonic() - t0) * 1000)


# ── Cascade ────────────────────────────────────────────────────────────────

@dataclass
class ReadResult:
    title: str = ""
    description: str = ""
    content: str = ""
    path: str = ""            # fast | reader | browser | "" when nothing worked
    html: str = ""            # raw HTML when the fast path fetched it (crawl reuses links)
    final_url: str = ""
    any_completed: bool = False  # some provider answered, even without content
    not_found: bool = False   # the site itself said the page does not exist (404/410)


@dataclass
class Trace:
    source: str
    url: str
    caller: str = ""
    fast_status: Optional[int] = None
    fast_error: Optional[str] = None
    fast_via_proxy: bool = False
    fast_chars: Optional[int] = None
    fast_ms: Optional[int] = None
    gate_decision: Optional[str] = None
    gate_reason: Optional[str] = None
    extractor: Optional[str] = None
    signals: dict = field(default_factory=dict)
    judge_ok: Optional[bool] = None
    judge_reason: Optional[str] = None
    judge_ms: Optional[int] = None
    reader_called: bool = False
    reader_chars: Optional[int] = None
    reader_ms: Optional[int] = None
    reader_error: Optional[str] = None
    browser_called: bool = False
    browser_via_proxy: bool = False
    browser_chars: Optional[int] = None
    browser_ms: Optional[int] = None
    browser_error: Optional[str] = None
    path: str = ""
    final_chars: int = 0
    total_ms: int = 0
    snippet: str = ""


ReaderFn = Callable[[str, str], Awaitable[dict]]
BrowserFn = Callable[[str, bool], Awaitable[dict]]
LogFn = Callable[[Trace], Awaitable[None]]


async def read_page(url: str, settings, *, reader_fn: ReaderFn, browser_fn: BrowserFn,
                    return_format: str = "markdown", source: str = "read", caller: str = "",
                    log_fn: Optional[LogFn] = None) -> ReadResult:
    t_start = time.monotonic()
    tr = Trace(source=source, url=url[:2000], caller=caller[:64])
    res = ReadResult(final_url=url)
    proxy = getattr(settings, "proxy_url", "") or None
    try:
        # 1. fast path
        t0 = time.monotonic()
        f = await fetch_html(url)
        if not f.html and f.error not in ("unsafe_url", "dns_unresolved") and \
                should_retry_via_proxy(url, f.status, f.error, bool(proxy)):
            tr.fast_via_proxy = True
            f = await fetch_html(url, proxy=proxy)
        tr.fast_status, tr.fast_error = f.status, f.error
        if f.error == "unsafe_url":
            raise UnsafeURL(url)
        # The origin itself says there is no such page: the reader and the browser
        # would spend 20 s each to say the same (agents guess raw GitHub paths).
        # Not for an unresolvable host: our DNS may miss what the reader resolves.
        if f.status in (404, 410):
            res.not_found = True
            tr.fast_ms = int((time.monotonic() - t0) * 1000)
            return res
        if f.html:
            extractor = getattr(settings, "extractor", "") or DEFAULT_EXTRACTOR
            if extractor not in EXTRACTORS:
                extractor = DEFAULT_EXTRACTOR
            # Off the event loop: trafilatura/readability are CPU, defuddle waits on node.
            name, title, text, sig, decision, reason = (await asyncio.to_thread(
                extract_best, f.html, f.final_url or url, return_format, extractor))[0]
            tr.extractor = name
            res.html, res.final_url, res.title = f.html, f.final_url or url, title
            res.any_completed = True
            tr.fast_chars, tr.signals = len(text), sig
            tr.gate_decision, tr.gate_reason = decision, reason
            if decision == "judge":
                tr.judge_ok, tr.judge_reason, tr.judge_ms = await judge(url, title, text, sig, settings)
                accepted = tr.judge_ok is True
            else:
                accepted = decision == "ok"
            if accepted:
                res.content, res.path = text, "fast"
        tr.fast_ms = int((time.monotonic() - t0) * 1000)

        # 2. reader
        if not res.path:
            tr.reader_called = True
            t0 = time.monotonic()
            try:
                r = await reader_fn(url, return_format)
                res.any_completed = True
                res.title = r.get("title") or res.title
                res.description = r.get("description", "") or res.description
                tr.reader_chars = len(r.get("content") or "")
                if len(r.get("content") or "") >= MIN_PROVIDER_TEXT:
                    res.content, res.path = r["content"], "reader"
            except Exception as e:
                tr.reader_error = f"{type(e).__name__}: {e}"[:200]
            tr.reader_ms = int((time.monotonic() - t0) * 1000)

        # 3. browser (direct, then proxy for foreign hosts)
        if not res.path:
            tr.browser_called = True
            t0 = time.monotonic()
            attempts = [] if tr.fast_error == "dns_unresolved" else \
                [False] + ([True] if proxy and not is_domestic(url) else [])
            for via_proxy in attempts:
                tr.browser_via_proxy = via_proxy
                try:
                    b = await browser_fn(url, via_proxy)
                    res.any_completed = True
                    res.title = res.title or b.get("title", "")
                    tr.browser_chars = len(b.get("content") or "")
                    if len(b.get("content") or "") >= MIN_PROVIDER_TEXT:
                        res.content, res.path = b["content"], "browser"
                        break
                except Exception as e:
                    tr.browser_error = f"{type(e).__name__}: {e}"[:200]
            tr.browser_ms = int((time.monotonic() - t0) * 1000)
        return res
    finally:
        tr.path = res.path
        tr.final_chars = len(res.content)
        tr.total_ms = int((time.monotonic() - t_start) * 1000)
        tr.snippet = res.content[:300]
        logger.info(f"page read {tr.source}: path={tr.path or 'none'} gate={tr.gate_decision}/{tr.gate_reason} "
                    f"judge={tr.judge_ok} {tr.final_chars} chars {tr.total_ms} ms {url[:120]}")
        if log_fn:
            asyncio.create_task(_safe_log(log_fn, tr))


class UnsafeURL(Exception):
    pass


async def _safe_log(log_fn: LogFn, tr: Trace) -> None:
    try:
        await log_fn(tr)
    except Exception as e:
        logger.warning(f"page_read_log write failed: {type(e).__name__}: {e}")
