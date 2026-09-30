"""
FastAPI server that provides Tavily-compatible API using SearXNG backend.

Endpoints:
- POST /search                       — Tavily-совместимый поиск
- POST /extract                      — Извлечение страницы в markdown (s/m/l/f)
- GET  /extract/{id}/{page}          — Пагинация для size=f
- POST /research                     — Запустить deep-research задачу (ephemeral Hermes)
- GET  /research/{job_id}            — Статус / готовый report.md
- GET  /research/{job_id}/logs       — Hermes stdout/stderr (для отладки)
- DELETE /research/{job_id}          — Cancel активной задачи
- GET  /health                       — health-check
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path as FSPath
from typing import Any, Literal

import aiohttp
import trafilatura
from fastapi import FastAPI, Header, HTTPException, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, constr
from sse_starlette.sse import EventSourceResponse

from tavily_client import TavilyResponse, TavilyResult
from config_loader import config
from orchestrator import Orchestrator, Job, JobStatus
import reader
import read_backends
import read_log
import roles
import media
import search_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Searcharvester", version="2.2.0")

# ---------- CORS ----------
# Frontend dev server is on :9762. Prod build served by the same origin or
# another port the user runs — allow anything on localhost by default, tighten
# via env var if needed.
_cors_origins = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:9762,http://127.0.0.1:9762,http://localhost:8000",
).split(",")
_allowed_origins = [o.strip() for o in _cors_origins if o.strip()]

# ---------- Request guard (stage 0 of docs/ui-and-visualization.md) ----------
# The API has no login and a job runs an agent with a shell. Two browser attacks
# reach a localhost API anyway:
# - a plain HTML form on any site POSTs here without a CORS preflight: every
#   state-changing request must carry X-Searcharvester-Client (a custom header
#   forces a preflight, which CORS then refuses for foreign origins), and a
#   present Origin must be one of ours;
# - DNS rebinding: a foreign name resolving to 127.0.0.1 is "same origin" for the
#   browser, so GETs (job list, reports, SSE) are readable: Host must be ours.
CLIENT_HEADER = "x-searcharvester-client"
_allowed_hosts = {
    h.strip().lower()
    for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],tavily-adapter").split(",")
    if h.strip()
}
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _host_allowed(host_header: str) -> bool:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8000
        host = host.split("]", 1)[0] + "]"
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host in _allowed_hosts


@app.middleware("http")
async def request_guard(request, call_next):
    from fastapi.responses import JSONResponse
    if not _host_allowed(request.headers.get("host", "")):
        return JSONResponse({"detail": "Host not allowed"}, status_code=400)
    if request.method in _MUTATING:
        origin = request.headers.get("origin")
        if origin is not None and origin not in _allowed_origins:
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        if request.headers.get(CLIENT_HEADER) != "1":
            return JSONResponse({"detail": f"{CLIENT_HEADER} header required"}, status_code=403)
    return await call_next(request)


# CORS goes on last so it wraps the guard: preflights are answered before it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Orchestrator singleton ----------

def _build_orchestrator() -> Orchestrator | None:
    """Build Orchestrator. v2.2+ runs `hermes acp` as a subprocess in the same
    container, so there's no Docker-daemon prereq. Returns None only if the
    `hermes` binary isn't on PATH (e.g. running outside the baked image)."""
    import shutil
    hermes_bin = os.environ.get("HERMES_BIN", "hermes")
    if shutil.which(hermes_bin) is None:
        logger.warning("%s not on PATH — /research disabled", hermes_bin)
        return None

    jobs_dir = FSPath(os.environ.get("JOBS_DIR", "/srv/searxng-docker/jobs"))
    jobs_dir.mkdir(parents=True, exist_ok=True)

    pass_env_keys = [
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "OLLAMA_API_KEY", "OLLAMA_BASE_URL",
        "NOUS_API_KEY",
    ]
    env = {k: os.environ[k] for k in pass_env_keys if k in os.environ}

    return Orchestrator(
        hermes_bin=hermes_bin,
        skills=[
            "searcharvester-deep-research",
            "searcharvester-search",
            "searcharvester-extract",
        ],
        jobs_dir=jobs_dir,
        env=env,
        adapter_url_for_hermes=os.environ.get(
            "ADAPTER_URL_FOR_HERMES", "http://localhost:8000"
        ),
        timeout_sec=int(os.environ.get("RESEARCH_TIMEOUT_SEC", "900")),
        max_concurrent=int(os.environ.get("MAX_CONCURRENT_JOBS", "12")),
        hermes_home=os.environ.get("HERMES_HOME", "/opt/data"),
        state_dir=FSPath(os.environ.get("STATE_DIR", str(jobs_dir.parent / "state"))),
    )


orchestrator: Orchestrator | None = _build_orchestrator()
model_catalog = roles.ModelCatalog.from_env()
media_ledger = media.Ledger(orchestrator._state_dir if orchestrator is not None else
                            FSPath(os.environ.get("STATE_DIR", "/srv/searxng-docker/state")))
MEDIA_CATEGORIES = {"images", "videos"}


# ---------- Read cascade ----------

_reader_settings = read_backends.ReaderSettings.from_env()
_ENV_PROXY = _reader_settings.proxy_url


def _live_reader_proxy() -> str:
    """The Settings page's reader proxy (over PROXY_URL) and extractor; applied to
    the shared settings object the read cascade already holds."""
    _reader_settings.proxy_url = search_settings.reader_proxy() or _ENV_PROXY
    _reader_settings.extractor = search_settings.extractor() or reader.DEFAULT_EXTRACTOR
    return _reader_settings.proxy_url
_reader_fn = read_backends.neuraldeep_reader(_reader_settings)
_browser_fn = read_backends.playwright_browser(_reader_settings)


async def _free_only(*_a, **_k):
    raise read_backends.ReaderUnavailable("free fast path only")
try:
    read_log.init()
except Exception as e:  # a read-only or missing jobs dir must not stop the API
    logger.warning("page_read_log init failed: %s", e)


# ---------- Extract constants ----------

SIZE_LIMITS: dict[str, int] = {"s": 5000, "m": 10000, "l": 25000}
PAGE_SIZE = 25000
EXTRACT_CACHE_TTL_SEC = 1800  # 30 минут

# id -> {"url", "title", "content", "created_at"}
_extract_cache: dict[str, dict[str, Any]] = {}


# ---------- Request models ----------

class SearchRequest(BaseModel):
    query: str
    max_results: int = 10
    include_raw_content: bool = False
    engines: str | None = Field(
        default=None,
        description="Через запятую: google,duckduckgo,brave,bing,... Пусто → дефолт из кода",
    )
    categories: str | None = Field(
        default=None,
        description="general|news|images|videos|map|music|it|science|files|social",
    )


class ExtractRequest(BaseModel):
    url: str
    size: Literal["s", "m", "l", "f"] = Field(
        default="m",
        description="s=5000, m=10000, l=25000 символов (обрезка), f=полный с пагинацией",
    )


# ---------- Helpers ----------

def _extract_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


def _gc_extract_cache() -> None:
    """Удаляет просроченные записи из in-memory кеша."""
    now = time.time()
    expired = [k for k, v in _extract_cache.items() if now - v["created_at"] > EXTRACT_CACHE_TTL_SEC]
    for k in expired:
        _extract_cache.pop(k, None)


def _extract_markdown(html: str) -> tuple[str, str]:
    """Возвращает (title, markdown_content). Бросает HTTPException, если контента нет."""
    content = trafilatura.extract(
        html,
        output_format="markdown",
        include_formatting=True,
        include_links=True,
        include_tables=True,
        favor_recall=True,
    )
    if not content:
        raise HTTPException(
            status_code=422,
            detail="Не удалось извлечь основной контент страницы (пусто после очистки)",
        )

    title = ""
    try:
        metadata = trafilatura.extract_metadata(html)
        if metadata and metadata.title:
            title = metadata.title
    except Exception:
        pass

    return title, content


async def _extract_markdown_for_url(url: str) -> tuple[str, str]:
    """(title, markdown) through the read cascade (reader.py): SSRF-checked fast path
    with a quality gate, then the remote reader and the optional browser.
    400 for internal or non-http URLs, 404 when the site says the page does not exist,
    422 when a page answered but has no content, 502 when nothing could open it. Error texts stay neutral (no backend names)."""
    _live_reader_proxy()
    try:
        res = await reader.read_page(
            url, _reader_settings, reader_fn=_reader_fn, browser_fn=_browser_fn,
            return_format="markdown", source="extract", log_fn=read_log.log,
        )
    except reader.UnsafeURL:
        raise HTTPException(status_code=400, detail="URL is not allowed (internal or non-http address)")
    if res.content:
        return res.title, res.content
    if res.not_found:
        raise HTTPException(status_code=404, detail="Page not found: the site says this address does not exist. Take addresses from search results instead of guessing them.")
    if res.any_completed:
        raise HTTPException(status_code=422, detail="The page has no readable main content")
    raise HTTPException(status_code=502, detail="Page reading is temporarily unavailable")


def _build_extract_response(
    extract_id: str,
    url: str,
    title: str,
    full_content: str,
    size: str,
    page: int = 1,
) -> dict[str, Any]:
    total_chars = len(full_content)

    if size == "f":
        total_pages = max(1, math.ceil(total_chars / PAGE_SIZE))
        if page > total_pages:
            raise HTTPException(
                status_code=404,
                detail=f"Страница {page} не существует (всего {total_pages})",
            )
        start = (page - 1) * PAGE_SIZE
        chunk = full_content[start : start + PAGE_SIZE]
        pages_info: dict[str, Any] = {
            "current": page,
            "total": total_pages,
            "page_size": PAGE_SIZE,
        }
        if page < total_pages:
            pages_info["next"] = f"/extract/{extract_id}/{page + 1}"
    else:
        limit = SIZE_LIMITS[size]
        chunk = full_content[:limit]
        pages_info = {"current": 1, "total": 1, "page_size": limit}

    return {
        "id": extract_id,
        "url": url,
        "title": title,
        "format": "md",
        "size": size,
        "content": chunk,
        "chars": len(chunk),
        "total_chars": total_chars,
        "pages": pages_info,
    }


# ---------- /search ----------

async def _fetch_raw_content(url: str) -> str | None:
    """raw_content for /search results: the read cascade's free fast path only
    (SSRF-checked, quality gate), no paid reader per search result."""
    _live_reader_proxy()
    try:
        res = await reader.read_page(
            url, _reader_settings, reader_fn=_free_only, browser_fn=_free_only,
            return_format="markdown", source="search_raw", log_fn=read_log.log,
        )
    except reader.UnsafeURL:
        return None
    return res.content or None


JOB_ID_RE = r"^[0-9a-f]{16}$"
JOB_ID = Path(..., pattern=JOB_ID_RE)


def _job_guard(job_id: str | None):
    """Loop guard of the research job a skill script works for (guard.py), or None
    for plain API callers."""
    if not job_id or orchestrator is None or not re.fullmatch(JOB_ID_RE, job_id):
        return None
    job = orchestrator.get(job_id)
    return job.guard if job is not None else None


def _guard_notice_response(query: str, notice: str, cached: dict | None) -> dict[str, Any]:
    body = dict(cached) if cached else TavilyResponse(
        query=query, results=[], response_time=0.0, request_id=str(uuid.uuid4())).model_dump()
    body["notice"] = notice
    return body


@app.post("/search")
async def search(
    request: SearchRequest,
    x_searcharvester_job: str | None = Header(default=None),
) -> dict[str, Any]:
    """Tavily-совместимый эндпойнт поиска."""
    guard = _job_guard(x_searcharvester_job)
    category = (request.categories or "general").strip().lower()
    # The same words as images or videos are another search, not a repeat.
    guard_key = f"__{category}__ {request.query}" if category in MEDIA_CATEGORIES else request.query
    if guard is not None:
        verdict, notice, cached = guard.on_search(guard_key)
        if verdict != "ok":
            logger.info("Search %s by loop guard: q=%r job=%s", verdict, request.query, x_searcharvester_job)
            return _guard_notice_response(request.query, notice, cached)
    start_time = time.time()
    request_id = str(uuid.uuid4())

    logger.info(
        "Search: q=%r engines=%s categories=%s raw=%s",
        request.query, request.engines, request.categories, request.include_raw_content,
    )

    searxng_params = {
        "q": request.query,
        "format": "json",
        "categories": category,
        "pageno": 1,
        "language": "auto",
        "safesearch": 1,
    }
    # The caller's engines, else the Settings page's for this category, else ours:
    # web engines for general; images and videos keep SearXNG's own engines of the
    # category (web engines return pages, not pictures).
    engines = request.engines or search_settings.default_engines(category) or (
        None if category in MEDIA_CATEGORIES else "google,duckduckgo,brave")
    if engines:
        searxng_params["engines"] = engines

    headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
        "User-Agent": "Mozilla/5.0 (compatible; TavilyBot/1.0)",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f"{config.searxng_url}/search",
                data=searxng_params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    raise HTTPException(status_code=500, detail="SearXNG request failed")
                searxng_data = await response.json()
        except aiohttp.TimeoutError:
            raise HTTPException(status_code=504, detail="SearXNG timeout")
        except HTTPException:
            raise
        except Exception as e:
            logger.error("SearXNG error: %s", e)
            raise HTTPException(status_code=500, detail="Search service unavailable")

    searxng_results = searxng_data.get("results", [])

    raw_contents: dict[str, str] = {}
    if request.include_raw_content and searxng_results:
        urls_to_scrape = [
            r["url"] for r in searxng_results[: request.max_results] if r.get("url")
        ]
        page_contents = await asyncio.gather(*[_fetch_raw_content(u) for u in urls_to_scrape],
                                             return_exceptions=True)
        for url, content in zip(urls_to_scrape, page_contents):
            if isinstance(content, str) and content:
                raw_contents[url] = content

    results: list[TavilyResult] = []
    for i, result in enumerate(searxng_results[: request.max_results]):
        if not result.get("url"):
            continue
        raw_content = raw_contents.get(result["url"]) if request.include_raw_content else None
        m = (media.items_from_results([result], category) or [{}])[0] if category in MEDIA_CATEGORIES else {}
        results.append(
            TavilyResult(
                url=result["url"],
                title=result.get("title", ""),
                content=result.get("content", ""),
                score=0.9 - (i * 0.05),
                raw_content=raw_content,
                img_src=m.get("src") if m.get("kind") == "image" else None,
                thumbnail=m.get("thumb"),
                duration=m.get("duration"),
            )
        )
    if category in MEDIA_CATEGORIES and guard is not None:
        # The job's allow list for /media: only what search handed to this job.
        try:
            media_ledger.record(x_searcharvester_job,
                                media.items_from_results(searxng_results[: request.max_results], category),
                                query=request.query)
        except Exception:
            logger.warning("media ledger write failed for %s", x_searcharvester_job, exc_info=True)

    response_time = time.time() - start_time

    response = TavilyResponse(
        query=request.query,
        follow_up_questions=None,
        answer=None,
        images=[r.img_src for r in results if r.img_src],
        results=results,
        response_time=response_time,
        request_id=request_id,
    )

    logger.info("Search done: %d results in %.2fs", len(results), response_time)
    body = response.model_dump()
    for r in body["results"]:   # media fields only where they mean something
        for k in ("img_src", "thumbnail", "duration"):
            if r.get(k) is None:
                r.pop(k, None)
    if guard is not None:
        guard.remember_search(guard_key, body)
    return body


# ---------- /extract ----------

@app.post("/extract")
async def extract(
    req: ExtractRequest,
    x_searcharvester_job: str | None = Header(default=None),
) -> dict[str, Any]:
    """Извлекает main-content страницы в markdown. Возвращает id для пагинации (size=f)."""
    guard = _job_guard(x_searcharvester_job)
    if guard is not None:
        verdict, notice = guard.on_extract(req.url)
        if verdict == "exhausted":
            logger.info("Extract refused by loop guard: url=%s job=%s", req.url, x_searcharvester_job)
            return {"id": None, "url": req.url, "title": "", "content": "", "notice": notice}
    _gc_extract_cache()
    extract_id = _extract_id(req.url)

    cached = _extract_cache.get(extract_id)
    if cached and cached["url"] == req.url:
        title, content = cached["title"], cached["content"]
    else:
        title, content = await _extract_markdown_for_url(req.url)
        _extract_cache[extract_id] = {
            "url": req.url,
            "title": title,
            "content": content,
            "created_at": time.time(),
        }

    return _build_extract_response(extract_id, req.url, title, content, req.size, page=1)


@app.get("/extract/{extract_id}/{page}")
async def extract_page(
    extract_id: str = Path(..., min_length=16, max_length=16),
    page: int = Path(..., ge=1),
) -> dict[str, Any]:
    """Возвращает page-ую страницу ранее извлечённого контента (только для size=f)."""
    _gc_extract_cache()
    cached = _extract_cache.get(extract_id)
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="id не найден или просрочен (TTL 30 мин). Повторите POST /extract.",
        )
    return _build_extract_response(
        extract_id, cached["url"], cached["title"], cached["content"], size="f", page=page,
    )


# ---------- /research ----------

class RoleChoice(BaseModel):
    # None keeps the default of the role (hermes-data/config.yaml model.default).
    model: constr(pattern=roles.MODEL_ID_RE.pattern) | None = None  # type: ignore[valid-type]
    reasoning: Literal["auto", "off", "low", "medium", "high"] | None = None


RoleName = Literal["lead", "researcher", "critic", "fact_checker"]


class ResearchRequest(BaseModel):
    query: constr(min_length=1, max_length=2000)  # type: ignore[valid-type]
    # quick: one agent, ~8 searches and page reads, for short factual questions;
    # deep: the lead + researchers + critic/fact-checker team.
    depth: Literal["quick", "deep"] = "deep"
    # Model and reasoning per role; a quick job uses only `lead`.
    models: dict[RoleName, RoleChoice] | None = None


class ResearchCreated(BaseModel):
    job_id: str
    status: str


class ResearchStatus(BaseModel):
    job_id: str
    status: str
    query: str
    depth: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_sec: float | None = None
    report: str | None = None
    error: str | None = None
    models: dict[str, Any] | None = None


def _ensure_orchestrator() -> Orchestrator:
    if orchestrator is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Research orchestrator is not available "
                "(hermes binary not found on PATH)."
            ),
        )
    return orchestrator


def _job_to_status(job: Job) -> ResearchStatus:
    return ResearchStatus(
        job_id=job.id,
        status=job.status.value,
        query=job.query,
        depth=job.depth,
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        duration_sec=job.duration_sec,
        report=job.report,
        error=job.error,
        models=job.models or None,
    )


def _job_phase(job: Job) -> str:
    """Cheap phase heuristic based on workspace contents.

    - queued / cancelled / failed / timeout / completed → pass-through of status
    - running without plan.md → "planning"
    - running with plan.md, no notes.md → "gather"
    - running with notes.md, no report.md → "synthesise"
    - running with report.md → "verify"  (the agent is writing the REPORT_SAVED marker now)
    """
    if job.status != JobStatus.running:
        return job.status.value
    ws = job.workspace_path
    if ws is None:
        return "running"
    try:
        if (ws / "report.md").exists():
            return "verify"
        if (ws / "notes.md").exists():
            return "synthesise"
        if (ws / "plan.md").exists():
            return "gather"
    except Exception:
        pass
    return "planning"


def _job_artifacts(job: Job) -> dict[str, int]:
    """Map artifact name → size in bytes, for debug pane in the UI."""
    if job.workspace_path is None:
        return {}
    out: dict[str, int] = {}
    for name in ("plan.md", "notes.md", "report.md", "hermes.log"):
        p = job.workspace_path / name
        try:
            if p.exists():
                out[name] = p.stat().st_size
        except Exception:
            pass
    return out


def _role_defaults() -> dict[str, dict[str, Any]]:
    return roles.defaults(os.environ.get("HERMES_HOME", "/opt/data"))


@app.get("/research/models")
async def research_models() -> dict[str, Any]:
    """What the UI offers per role: the gateway's chat models with tool calls
    (id, reasoning flag, context) and the default of each role."""
    models = await asyncio.to_thread(model_catalog.models)
    return {"roles": list(roles.ROLES), "reasoning": list(roles.REASONING),
            "defaults": _role_defaults(), "models": models, "error": model_catalog.error}


@app.post("/research", response_model=ResearchCreated, status_code=202)
async def research_create(req: ResearchRequest) -> dict[str, str]:
    orch = _ensure_orchestrator()
    requested = {r: c.model_dump(exclude_none=True) for r, c in (req.models or {}).items()}
    picked = {c["model"] for c in requested.values() if c.get("model")}
    if picked:
        # Refuse a model the gateway does not serve now, not an hour into the job.
        # No list (gateway down, no capability) means no check: the job will say.
        known = await asyncio.to_thread(model_catalog.ids)
        unknown = sorted(picked - known) if known else []
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown model: {', '.join(unknown)}")
    job_id = await orch.spawn(query=req.query, depth=req.depth,
                              models=roles.resolve(requested, _role_defaults()))
    return {"job_id": job_id, "status": "queued"}


@app.get("/research/{job_id}", response_model=ResearchStatus)
async def research_get(job_id: str = JOB_ID) -> ResearchStatus:
    orch = _ensure_orchestrator()
    job = orch.get(job_id)
    if job is not None:
        return _job_to_status(job)
    meta = orch.load_meta(job_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return ResearchStatus(
        job_id=job_id, status=meta.get("status", "interrupted"), query=meta.get("query", ""),
        depth=meta.get("depth"), started_at=meta.get("started_at"), finished_at=meta.get("finished_at"),
        duration_sec=meta.get("duration_sec"), report=orch.load_report(job_id, meta), error=meta.get("error"),
        models=meta.get("models") or None,
    )


@app.get("/research")
async def research_list(limit: int = 50) -> dict[str, Any]:
    """Jobs for the sidebar, newest first: live ones and finished ones from disk."""
    orch = _ensure_orchestrator()
    return {"jobs": orch.list_jobs(limit=max(1, min(limit, 200)))}


@app.get("/research/{job_id}/logs")
async def research_logs(job_id: str = JOB_ID) -> dict[str, str]:
    orch = _ensure_orchestrator()
    job = orch.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    logs = orch.read_logs(job_id)
    if logs is None:
        raise HTTPException(status_code=404, detail="Logs not available yet")
    return {"job_id": job_id, "logs": logs}


@app.get("/research/{job_id}/events")
async def research_events(request: Request, job_id: str = JOB_ID, after: int = 0):
    """SSE stream of typed agent events for a research job.

    Each event is a normalized dict — see events.Event for schema:
        {ts, job_id, agent_id, parent_id, type, payload}

    `type` values: spawn | thought | message | tool_call | tool_result |
                   plan | commands | note | done

    The stream replays the full history on subscribe, then appends live.
    Closes after emitting the final `done` event (status == completed /
    failed / timeout / cancelled).
    """
    orch = _ensure_orchestrator()
    job = orch.get(job_id)
    # Resume point: Last-Event-ID (EventSource reconnect) or ?after=<seq>.
    try:
        after = max(after, int(request.headers.get("last-event-id") or 0))
    except ValueError:
        pass
    if job is None:
        meta = orch.load_meta(job_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

        async def disk_stream():
            for d in orch.disk_events(job_id):
                if d["seq"] > after:
                    yield {"event": d.get("type", "note"), "id": str(d["seq"]),
                           "data": json.dumps(d, ensure_ascii=False)}
            yield {"event": "status", "data": json.dumps({
                "job_id": job_id, "status": meta.get("status"), "duration_sec": meta.get("duration_sec"),
                "has_report": orch.load_report(job_id, meta) is not None, "error": meta.get("error"),
            }, ensure_ascii=False)}
        return EventSourceResponse(disk_stream())

    async def event_stream():
        async for ev in orch.subscribe(job_id, after=after):
            yield {
                "event": ev.type,
                "id": str(ev.seq),
                "data": json.dumps(ev.to_dict(), ensure_ascii=False),
            }
        # Final status event (handy for clients that only care about the
        # outcome and don't want to parse the last `done` payload).
        final = orch.get(job_id)
        if final is not None:
            yield {
                "event": "status",
                "data": json.dumps({
                    "job_id": final.id,
                    "status": final.status.value,
                    "duration_sec": final.duration_sec,
                    "has_report": final.report is not None,
                    "error": final.error,
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_stream())


@app.get("/research/{job_id}/snapshot")
async def research_snapshot(job_id: str = JOB_ID) -> dict[str, Any]:
    """Return the full event log so far (no streaming). Useful for
    non-SSE clients or reconnecting UIs that already got a `since_ts`."""
    orch = _ensure_orchestrator()
    job = orch.get(job_id)
    if job is None:
        meta = orch.load_meta(job_id)
        if meta is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return {"job_id": job_id, "status": meta.get("status"), "phase": meta.get("status"),
                "artifacts": {}, "events": orch.disk_events(job_id)}
    events = orch.snapshot(job_id)
    return {
        "job_id": job_id,
        "status": job.status.value,
        "phase": _job_phase(job),
        "artifacts": _job_artifacts(job),
        "events": [e.to_dict() for e in events],
    }


@app.get("/research/{job_id}/media")
async def research_media(job_id: str = JOB_ID) -> dict[str, Any]:
    """Images and videos search handed to this job, first seen first; the UI
    shows them through /media."""
    return {"job_id": job_id, "items": await asyncio.to_thread(media_ledger.items, job_id)}


_MEDIA_HEADERS = {
    "Cache-Control": "private, max-age=86400",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
    "Content-Disposition": "inline",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


@app.get("/media")
async def media_get(job: constr(pattern=JOB_ID_RE), src: constr(min_length=8, max_length=2048)):  # type: ignore[valid-type]
    """One image or preview of a job: only a src search gave this job (its ledger),
    fetched by the adapter the safe way and cached; see media.py."""
    from fastapi.responses import FileResponse, Response
    src = await asyncio.to_thread(media_ledger.resolve, job, src)
    if src is None:
        raise HTTPException(status_code=404, detail="not a media item of this job")
    hit = await asyncio.to_thread(media_ledger.cached, job, src)
    if hit:
        return FileResponse(hit[0], media_type=hit[1], headers=_MEDIA_HEADERS)
    proxy = _live_reader_proxy() or None
    try:
        img = await media.fetch_image(src)
    except media.MediaError as e:
        if not (proxy and e.status in (502, 504) and not reader.is_domestic(src)):
            raise HTTPException(status_code=e.status, detail=e.detail)
        try:
            img = await media.fetch_image(src, proxy=proxy)   # foreign host refused our IP
        except media.MediaError as e2:
            raise HTTPException(status_code=e2.status, detail=e2.detail)
    f = media_ledger.cache_file(job, src, img.ext)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_bytes(img.body)
        os.replace(tmp, f)
    except OSError:
        logger.warning("media cache write failed for %s", job, exc_info=True)
    return Response(content=img.body, media_type=media.TYPES[img.ext], headers=_MEDIA_HEADERS)


@app.delete("/research/{job_id}")
async def research_cancel(job_id: str = JOB_ID) -> dict[str, Any]:
    orch = _ensure_orchestrator()
    job = orch.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    cancelled = await orch.cancel(job_id)
    return {"job_id": job_id, "cancelled": cancelled, "status": orch.get(job_id).status.value}


# ---------- /health ----------

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "searcharvester",
        "version": "2.2.0",
        "orchestrator": "available" if orchestrator is not None else "unavailable",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.server_host, port=config.server_port)
