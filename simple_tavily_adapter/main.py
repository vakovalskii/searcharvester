"""
FastAPI server that provides Tavily-compatible API using SearXNG backend.

Endpoints:
- POST /search                       — Tavily-compatible search
- POST /extract                      — Fetch a URL and return markdown (s/m/l/f presets)
- GET  /extract/{id}/{page}          — Pagination for size=f
- POST /research                     — Start a deep-research job (ephemeral Hermes)
- GET  /research/{job_id}            — Job status / finished report
- GET  /research/{job_id}/logs       — Hermes stdout/stderr (for debugging)
- DELETE /research/{job_id}          — Cancel an active job
- GET  /health                       — health-check
"""
import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path as FSPath
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import aiohttp
import trafilatura
from fastapi import FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from fastmcp import FastMCP
from pydantic import BaseModel, Field, constr
from sse_starlette.sse import EventSourceResponse
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from tavily_client import TavilyResponse, TavilyResult
from config_loader import config
from orchestrator import Orchestrator, Job, JobStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_http_session: aiohttp.ClientSession | None = None
_MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "http")
_SEARCH_TIMEOUT_SEC = int(os.environ.get("SEARCH_TIMEOUT_SEC", "30"))
_EXTRACT_TIMEOUT_SEC = int(os.environ.get("EXTRACT_TIMEOUT_SEC", "30"))


@asynccontextmanager
async def _mcp_lifespan(server):
    """Runs under both HTTP and stdio transports — the only place _http_session is created."""
    global _http_session
    _http_session = aiohttp.ClientSession()
    yield
    await _http_session.close()
    _http_session = None


_LLM_ENV_KEYS = [
    "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "OLLAMA_BASE_URL", "NOUS_API_KEY",
]
_llm_configured = any(os.environ.get(k) for k in _LLM_ENV_KEYS)
_mcp_instructions = (
    "Use searcharvester_search to find information on the web. "
    "Use searcharvester_extract to read the full content of specific URLs. "
    "Use searcharvester_extract_page to read subsequent pages of long documents "
    "(requires id from searcharvester_extract with size=f). "
    + (
        "Use searcharvester_research for deep multi-source research that returns a full cited report (slow, takes minutes)."
        if _llm_configured else ""
    )
)
mcp = FastMCP(
    name="searcharvester",
    lifespan=_mcp_lifespan,
    instructions=_mcp_instructions,
)

_mcp_allowed_hosts = [
    h.strip()
    for h in os.environ.get(
        "MCP_ALLOWED_HOSTS", "localhost,127.0.0.1,localhost:*,127.0.0.1:*"
    ).split(",")
    if h.strip()
]
_mcp_app = mcp.http_app(path="/", middleware=[
    Middleware(TrustedHostMiddleware, allowed_hosts=_mcp_allowed_hosts)
])


async def _startup_health_check() -> None:
    try:
        async with _http_session.get(
            f"{config.searxng_url}/",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status < 500:
                logger.info("SearXNG reachable at %s (HTTP %d)", config.searxng_url, resp.status)
            else:
                logger.warning("Startup: SearXNG at %s returned HTTP %d", config.searxng_url, resp.status)
    except Exception as exc:
        logger.warning("Startup: SearXNG at %s unreachable: %s", config.searxng_url, exc)


async def _with_retry(make_coro, *, backoff: float = 1.0):
    for attempt in range(2):
        try:
            return await make_coro()
        except (aiohttp.ClientConnectionError, aiohttp.ServerConnectionError) as exc:
            if attempt == 1:
                raise
            logger.warning("Transient connection error, retrying: %s", exc)
            await asyncio.sleep(backoff)
        except HTTPException as exc:
            if exc.status_code in (502, 504) and attempt == 0:
                logger.warning("Transient HTTP %d, retrying", exc.status_code)
                await asyncio.sleep(backoff)
            else:
                raise


@asynccontextmanager
async def _lifespan(fastapi_app):
    async with _mcp_app.lifespan(fastapi_app):
        # _http_session is now live (created by _mcp_lifespan above)
        await _startup_health_check()
        yield


app = FastAPI(title="Searcharvester", version="2.2.0", lifespan=_lifespan)

# ---------- CORS ----------
# Frontend dev server is on :9762. Prod build served by the same origin or
# another port the user runs — allow anything on localhost by default, tighten
# via env var if needed.
_cors_origins = os.environ.get(
    "CORS_ORIGINS",
    "http://localhost:9762,http://127.0.0.1:9762,http://localhost:8000",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
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
        hermes_home=os.environ.get("HERMES_HOME", "/opt/data"),
    )


orchestrator: Orchestrator | None = _build_orchestrator()

_research_enabled = orchestrator is not None and _llm_configured
if not _research_enabled:
    logger.info("Research tool disabled: no LLM credentials configured")



# ---------- Extract constants ----------

SIZE_LIMITS: dict[str, int] = {"s": 5000, "m": 10000, "l": 25000}
PAGE_SIZE = 25000
EXTRACT_CACHE_TTL_SEC = 1800  # 30 minutes
EXTRACT_MAX_CONCURRENCY = int(os.environ.get("EXTRACT_MAX_CONCURRENCY", "5"))  # caps parallel URL fetches — higher values risk rate-limiting by target hosts
_extract_semaphore = asyncio.Semaphore(EXTRACT_MAX_CONCURRENCY)


def _validate_url(url: str) -> None:
    """Guard against SSRF — rejects non-HTTP schemes, localhost, and private/reserved IP ranges."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"URL scheme '{parsed.scheme}' not allowed — use http or https")
    hostname = parsed.hostname or ""
    if hostname.lower() in ("localhost", ""):
        raise ValueError(f"URL hostname '{hostname}' not allowed")
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return  # hostname is a domain name, not an IP literal — allow through
    if addr.is_private:
        raise ValueError(f"URL resolves to a private/reserved address: {hostname}")

# id -> {"url", "title", "content", "created_at"}
_extract_cache: dict[str, dict[str, Any]] = {}


# ---------- Request models ----------

class SearchRequest(BaseModel):
    query: str
    max_results: int = 10
    include_raw_content: bool = False
    engines: str | None = Field(
        default=None,
        description="Comma-separated: google,duckduckgo,brave,bing,... Empty → uses configured default.",
    )
    categories: str | None = Field(
        default=None,
        description="general|news|images|videos|map|music|it|science|files|social",
    )


class ExtractRequest(BaseModel):
    url: str
    size: Literal["s", "m", "l", "f"] = Field(
        default="m",
        description="s=5 000, m=10 000, l=25 000 chars (truncated); f=full content with pagination",
    )


# ---------- Helpers ----------

def _extract_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


def _gc_extract_cache() -> None:
    now = time.time()
    expired = [k for k, v in _extract_cache.items() if now - v["created_at"] > EXTRACT_CACHE_TTL_SEC]
    for k in expired:
        _extract_cache.pop(k, None)


async def _fetch_html(url: str) -> str:
    async def _do():
        async with _http_session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=_EXTRACT_TIMEOUT_SEC),
            headers={"User-Agent": config.scraper_user_agent},
            allow_redirects=True,
        ) as response:
            if response.status != 200:
                raise HTTPException(
                    status_code=response.status,
                    detail=f"Failed to fetch {url}: HTTP {response.status}",
                )
            return await response.text()
    return await _with_retry(_do)


def _extract_markdown(html: str) -> tuple[str, str]:
    """Return (title, markdown_content). Raises HTTPException(422) if trafilatura yields nothing."""
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
            detail="Failed to extract main content from page (empty after cleanup)",
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
    html = await _fetch_html(url)
    return _extract_markdown(html)


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
                detail=f"Page {page} does not exist (total: {total_pages})",
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
    """Scrapes a page and returns markdown content (trafilatura) or None on error."""
    try:
        async with _http_session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=config.scraper_timeout),
            headers={"User-Agent": config.scraper_user_agent},
            allow_redirects=True,
        ) as response:
            if response.status != 200:
                return None
            html = await response.text()
    except Exception:
        return None

    try:
        content = trafilatura.extract(
            html,
            output_format="markdown",
            include_formatting=True,
            include_links=True,
            favor_recall=True,
        )
    except Exception:
        return None

    if not content:
        return None

    if len(content) > config.scraper_max_length:
        content = content[: config.scraper_max_length] + "..."
    return content


async def _execute_search(
    query: str,
    max_results: int,
    include_raw_content: bool,
    engines: str | None,
    categories: str | None,
) -> list[TavilyResult]:
    """Query SearXNG and optionally scrape raw content. Raises HTTPException on failure."""
    searxng_params = {
        "q": query,
        "format": "json",
        "categories": categories or "general",
        "engines": engines or config.default_engines,
        "pageno": 1,
        "language": "auto",
        "safesearch": 1,
    }
    headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Real-IP": "127.0.0.1",
        "User-Agent": "Mozilla/5.0 (compatible; TavilyBot/1.0)",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    async def _do_search():  # inner closure so _with_retry can re-invoke it on transient failure
        async with _http_session.post(
            f"{config.searxng_url}/search",
            data=searxng_params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=_SEARCH_TIMEOUT_SEC),
        ) as response:
            if response.status in (502, 504):
                raise HTTPException(status_code=response.status, detail="SearXNG upstream error")
            if response.status != 200:
                raise HTTPException(status_code=500, detail=f"SearXNG request failed (HTTP {response.status})")
            return await response.json()

    try:
        searxng_data = await _with_retry(_do_search)
    except (aiohttp.ServerTimeoutError, asyncio.TimeoutError):
        raise HTTPException(status_code=504, detail="SearXNG timeout")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("SearXNG error: %s", e)
        raise HTTPException(status_code=500, detail="Search service unavailable")

    searxng_results = searxng_data.get("results", [])

    raw_contents: dict[str, str] = {}
    if include_raw_content and searxng_results:
        urls_to_scrape = [
            r["url"] for r in searxng_results[:max_results] if r.get("url")
        ]
        tasks = [_fetch_raw_content(u) for u in urls_to_scrape]
        page_contents = await asyncio.gather(*tasks, return_exceptions=True)
        for url, content in zip(urls_to_scrape, page_contents):
            if isinstance(content, str) and content:
                raw_contents[url] = content

    results: list[TavilyResult] = []
    for i, result in enumerate(searxng_results[:max_results]):
        if not result.get("url"):
            continue
        results.append(
            TavilyResult(
                url=result["url"],
                title=result.get("title", ""),
                content=result.get("content", ""),
                score=0.9 - (i * 0.05),
                raw_content=raw_contents.get(result["url"]) if include_raw_content else None,
            )
        )

    return results


@app.post("/search")
async def search(request: SearchRequest) -> dict[str, Any]:
    """Tavily-compatible search endpoint."""
    start_time = time.time()
    request_id = str(uuid.uuid4())
    logger.info(
        "Search: q=%r engines=%s categories=%s raw=%s",
        request.query, request.engines, request.categories, request.include_raw_content,
    )
    results = await _execute_search(
        query=request.query,
        max_results=request.max_results,
        include_raw_content=request.include_raw_content,
        engines=request.engines,
        categories=request.categories,
    )
    response_time = time.time() - start_time
    response = TavilyResponse(
        query=request.query,
        follow_up_questions=None,
        answer=None,
        images=[],
        results=results,
        response_time=response_time,
        request_id=request_id,
    )
    logger.info("Search done: %d results in %.2fs", len(results), response_time)
    return response.model_dump()


# ---------- /extract ----------

@app.post("/extract")
async def extract(req: ExtractRequest) -> dict[str, Any]:
    """Fetch a URL and return its main content as markdown. Returns an id for pagination (size=f)."""
    try:
        _validate_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
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
    """Return a subsequent page of previously extracted content (size=f only)."""
    _gc_extract_cache()
    cached = _extract_cache.get(extract_id)
    if not cached:
        raise HTTPException(
            status_code=404,
            detail="id not found or expired (TTL 30 min). Repeat POST /extract.",
        )
    return _build_extract_response(
        extract_id, cached["url"], cached["title"], cached["content"], size="f", page=page,
    )


# ---------- /research ----------

class ResearchRequest(BaseModel):
    query: constr(min_length=1, max_length=2000)  # type: ignore[valid-type]


class ResearchCreated(BaseModel):
    job_id: str
    status: str


class ResearchStatus(BaseModel):
    job_id: str
    status: str
    query: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_sec: float | None = None
    report: str | None = None
    error: str | None = None


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
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        duration_sec=job.duration_sec,
        report=job.report,
        error=job.error,
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


@app.post("/research", response_model=ResearchCreated, status_code=202)
async def research_create(req: ResearchRequest) -> dict[str, str]:
    orch = _ensure_orchestrator()
    job_id = await orch.spawn(query=req.query)
    return {"job_id": job_id, "status": "queued"}


@app.get("/research/{job_id}", response_model=ResearchStatus)
async def research_get(job_id: str) -> ResearchStatus:
    orch = _ensure_orchestrator()
    job = orch.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return _job_to_status(job)


@app.get("/research/{job_id}/logs")
async def research_logs(job_id: str) -> dict[str, str]:
    orch = _ensure_orchestrator()
    job = orch.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    logs = orch.read_logs(job_id)
    if logs is None:
        raise HTTPException(status_code=404, detail="Logs not available yet")
    return {"job_id": job_id, "logs": logs}


@app.get("/research/{job_id}/events")
async def research_events(job_id: str):
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
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    async def event_stream():
        async for ev in orch.subscribe(job_id):
            yield {
                "event": ev.type,
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
async def research_snapshot(job_id: str) -> dict[str, Any]:
    """Return the full event log so far (no streaming). Useful for
    non-SSE clients or reconnecting UIs that already got a `since_ts`."""
    orch = _ensure_orchestrator()
    job = orch.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    events = orch.snapshot(job_id)
    return {
        "job_id": job_id,
        "status": job.status.value,
        "phase": _job_phase(job),
        "artifacts": _job_artifacts(job),
        "events": [e.to_dict() for e in events],
    }


@app.delete("/research/{job_id}")
async def research_cancel(job_id: str) -> dict[str, Any]:
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


# ---------- MCP tools ----------

@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True, "title": "Web Search"})
async def searcharvester_search(
    query: Annotated[str, Field(min_length=1, description="Search keywords or question")],
    max_results: Annotated[int, Field(ge=1, le=20, description="Number of results to return (1–20)")] = 5,
    topic: Annotated[Literal["general", "news"], Field(description="Search category; 'news' targets news engines")] = "general",
    include_raw_content: Annotated[bool, Field(description="Fetch full page markdown for each result (slower)")] = False,
    search_depth: Annotated[Literal["basic", "advanced"], Field(description="'advanced' forces include_raw_content=True")] = "basic",
    engines: Annotated[str | None, Field(description="Comma-separated engine list, e.g. 'google,brave,duckduckgo'. Defaults to server config.")] = None,
) -> dict[str, Any]:
    """Search the web and return ranked results with titles, URLs, and snippets. Use for current information, facts, and general queries. To read the full content of a specific page, use searcharvester_extract instead."""
    logger.info("MCP search: q=%r max_results=%d depth=%s raw=%s engines=%s", query, max_results, search_depth, include_raw_content, engines)
    if search_depth == "advanced":
        include_raw_content = True
    try:
        results = await _execute_search(
            query=query,
            max_results=max_results,
            include_raw_content=include_raw_content,
            engines=engines,
            categories=topic,
        )
    except HTTPException as exc:
        raise RuntimeError(f"Search failed: {exc.detail}") from exc
    logger.info("MCP search done: %d results", len(results))
    return {
        "query": query,
        "result_count": len(results),
        "results": [
            {
                "rank": i + 1,
                "url": r.url,
                "title": r.title,
                "content": r.content,
                **({"raw_content": r.raw_content} if r.raw_content is not None else {}),
            }
            for i, r in enumerate(results)
        ],
    }


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True, "title": "Extract Page Content"})
async def searcharvester_extract(
    urls: Annotated[list[str], Field(min_length=1, description="One or more URLs to extract (fetched in parallel)")],
    extract_depth: Annotated[Literal["basic", "advanced"], Field(description="Content depth: basic → 10k chars, advanced → 25k chars")] = "basic",
) -> dict[str, Any]:
    """Fetch one or more URLs and return their main content as clean markdown. Navigation, ads, and boilerplate are stripped. Use when you have specific URLs to read. For discovering URLs, use searcharvester_search instead."""
    logger.info("MCP extract: %d URL(s) depth=%s", len(urls), extract_depth)
    size = "m" if extract_depth == "basic" else "l"

    failed: list[dict[str, Any]] = []
    valid_urls: list[str] = []
    for url in urls:
        try:
            _validate_url(url)
            valid_urls.append(url)
        except ValueError as exc:
            failed.append({"url": url, "error": str(exc)})

    async def _extract_one(url: str) -> tuple[str, dict[str, Any] | None, str | None]:
        _gc_extract_cache()
        extract_id = _extract_id(url)
        cached = _extract_cache.get(extract_id)
        try:
            if cached and cached["url"] == url:
                title, content = cached["title"], cached["content"]
            else:
                title, content = await _extract_markdown_for_url(url)
                _extract_cache[extract_id] = {
                    "url": url,
                    "title": title,
                    "content": content,
                    "created_at": time.time(),
                }
            resp = _build_extract_response(extract_id, url, title, content, size)
            return url, resp, None
        except HTTPException as exc:
            # Expected failures (404, 422, etc.) are reported in the failed list,
            # not raised — callers can still inspect why each URL failed.
            return url, None, f"HTTP {exc.status_code}: {exc.detail}"
        # Generic exceptions (connection errors, unexpected crashes) propagate up;
        # asyncio.gather captures them via return_exceptions=True.

    async def _extract_one_limited(url: str) -> tuple[str, dict[str, Any] | None, str | None]:
        async with _extract_semaphore:
            return await _extract_one(url)

    outcomes = await asyncio.gather(
        *[_extract_one_limited(u) for u in valid_urls],
        return_exceptions=True,
    )

    results_list: list[dict[str, Any]] = []
    hard_failures = 0
    for url, outcome in zip(valid_urls, outcomes):
        if isinstance(outcome, Exception):
            failed.append({"url": url, "error": str(outcome)})
            hard_failures += 1
        else:
            _, resp, error = outcome
            if resp is not None:
                results_list.append({
                    "url": url,
                    "title": resp["title"],
                    "content": resp["content"],
                    "truncated": resp["chars"] < resp["total_chars"],
                    "chars": resp["chars"],
                    "total_chars": resp["total_chars"],
                })
            else:
                failed.append({"url": url, "error": error})

    if not results_list and hard_failures == len(valid_urls) and hard_failures > 0:
        raise RuntimeError(
            f"All {len(failed)} URL(s) failed. First error: {failed[0]['error']}"
        )

    logger.info("MCP extract done: %d ok, %d failed", len(results_list), len(failed))
    return {"results": results_list, "failed": failed}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True, "title": "Extract Page (Paginated)"})
async def searcharvester_extract_page(
    id: Annotated[str, Field(min_length=16, max_length=16, description="Extract ID from a prior searcharvester_extract call with size=f")],
    page: Annotated[int, Field(ge=1, description="Page number (1-indexed)")],
) -> dict[str, Any]:
    """Read a specific page of a previously extracted long document. The id comes from a prior searcharvester_extract call — only meaningful when the document was longer than 25k chars."""
    _gc_extract_cache()
    cached = _extract_cache.get(id)
    if not cached:
        raise RuntimeError(
            f"Extract id {id!r} not found or expired (TTL 30 min). Call searcharvester_extract first."
        )
    try:
        return _build_extract_response(id, cached["url"], cached["title"], cached["content"], size="f", page=page)
    except HTTPException as exc:
        raise RuntimeError(f"Page {page} out of range: {exc.detail}") from exc


if _research_enabled:
    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True, "title": "Deep Research"})
    async def searcharvester_research(
        query: Annotated[str, Field(min_length=1, description="Research question or topic")],
        timeout_sec: Annotated[int, Field(ge=60, le=1800, description="Max wait time in seconds (60–1800)")] = 900,
    ) -> dict[str, Any]:
        """Run a deep research job: searches multiple sources, extracts content, and returns a full cited markdown report. This is slow (minutes). Use for thorough research questions, not quick lookups. Requires the research orchestrator (hermes) to be running."""
        job_id = await orchestrator.spawn(query=query)
        logger.info("MCP research job %s started for query: %r", job_id, query)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_sec
        terminal = {JobStatus.failed, JobStatus.timeout, JobStatus.cancelled}
        while loop.time() < deadline:
            await asyncio.sleep(5)
            job = orchestrator.get(job_id)
            if job is None:
                raise RuntimeError(f"Research job {job_id} disappeared unexpectedly.")
            if job.status == JobStatus.completed:
                logger.info("MCP research job %s completed in %.1fs", job_id, job.duration_sec or 0)
                return {"job_id": job_id, "status": "completed", "report": job.report}
            if job.status in terminal:
                raise RuntimeError(
                    f"Research job {job_id} ended with status '{job.status.value}': {job.error or 'no details'}"
                )
        await orchestrator.cancel(job_id)
        raise RuntimeError(
            f"Research timed out after {timeout_sec}s. Job {job_id} was cancelled."
        )


app.mount("/mcp", _mcp_app)


if __name__ == "__main__":
    if _MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(app, host=config.server_host, port=config.server_port)
