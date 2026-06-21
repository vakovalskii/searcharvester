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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path as FSPath
from typing import Any, Literal

import aiohttp
import trafilatura
from fastapi import FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, constr
from sse_starlette.sse import EventSourceResponse

from tavily_client import TavilyResponse, TavilyResult
from config_loader import config
from orchestrator import Orchestrator, Job, JobStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Searcharvester", version="2.2.0")

EVOLINK_BASE_URL = "https://direct.evolink.ai/v1"

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


def _llm_env() -> dict[str, str]:
    pass_env_keys = [
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "OLLAMA_API_KEY", "OLLAMA_BASE_URL",
        "NOUS_API_KEY",
    ]
    env = {k: os.environ[k] for k in pass_env_keys if k in os.environ}

    if "EVOLINK_API_KEY" in os.environ:
        env.setdefault("OPENAI_API_KEY", os.environ["EVOLINK_API_KEY"])
        env.setdefault(
            "OPENAI_BASE_URL",
            os.environ.get("EVOLINK_BASE_URL", EVOLINK_BASE_URL),
        )

    return env


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

    return Orchestrator(
        hermes_bin=hermes_bin,
        skills=[
            "searcharvester-deep-research",
            "searcharvester-search",
            "searcharvester-extract",
        ],
        jobs_dir=jobs_dir,
        env=_llm_env(),
        adapter_url_for_hermes=os.environ.get(
            "ADAPTER_URL_FOR_HERMES", "http://localhost:8000"
        ),
        timeout_sec=int(os.environ.get("RESEARCH_TIMEOUT_SEC", "900")),
        hermes_home=os.environ.get("HERMES_HOME", "/opt/data"),
    )


orchestrator: Orchestrator | None = _build_orchestrator()


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


async def _fetch_html(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=config.scraper_timeout),
        headers={"User-Agent": config.scraper_user_agent},
        allow_redirects=True,
    ) as response:
        if response.status != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Не удалось скачать {url}: HTTP {response.status}",
            )
        return await response.text()


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
    async with aiohttp.ClientSession() as session:
        html = await _fetch_html(session, url)
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

async def _fetch_raw_content(session: aiohttp.ClientSession, url: str) -> str | None:
    """Скрапит страницу, возвращает markdown-контент (trafilatura) или None при ошибке."""
    try:
        async with session.get(
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


@app.post("/search")
async def search(request: SearchRequest) -> dict[str, Any]:
    """Tavily-совместимый эндпойнт поиска."""
    start_time = time.time()
    request_id = str(uuid.uuid4())

    logger.info(
        "Search: q=%r engines=%s categories=%s raw=%s",
        request.query, request.engines, request.categories, request.include_raw_content,
    )

    searxng_params = {
        "q": request.query,
        "format": "json",
        "categories": request.categories or "general",
        "engines": request.engines or "google,duckduckgo,brave",
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
        async with aiohttp.ClientSession() as scrape_session:
            tasks = [_fetch_raw_content(scrape_session, u) for u in urls_to_scrape]
            page_contents = await asyncio.gather(*tasks, return_exceptions=True)
            for url, content in zip(urls_to_scrape, page_contents):
                if isinstance(content, str) and content:
                    raw_contents[url] = content

    results: list[TavilyResult] = []
    for i, result in enumerate(searxng_results[: request.max_results]):
        if not result.get("url"):
            continue
        raw_content = raw_contents.get(result["url"]) if request.include_raw_content else None
        results.append(
            TavilyResult(
                url=result["url"],
                title=result.get("title", ""),
                content=result.get("content", ""),
                score=0.9 - (i * 0.05),
                raw_content=raw_content,
            )
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
    """Извлекает main-content страницы в markdown. Возвращает id для пагинации (size=f)."""
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.server_host, port=config.server_port)
