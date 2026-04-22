"""
FastAPI server that provides Tavily-compatible API using SearXNG backend.

Endpoints:
- POST /search          — Tavily-совместимый поиск (ссылки + сниппеты + опц. raw_content)
- POST /extract         — Извлечение контента страницы в markdown (s/m/l/f + пагинация)
- GET  /extract/{id}/{page} — Следующие страницы для size=f
- GET  /health          — health-check
"""
import asyncio
import hashlib
import logging
import math
import time
import uuid
from typing import Any, Literal

import aiohttp
import trafilatura
from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel, Field

from tavily_client import TavilyResponse, TavilyResult
from config_loader import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SearXNG Tavily Adapter", version="2.0.0")


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


# ---------- /health ----------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "searxng-tavily-adapter", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.server_host, port=config.server_port)
