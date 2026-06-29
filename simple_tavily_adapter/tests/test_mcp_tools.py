"""Tests for MCP tool handlers (searcharvester_search, _extract, _research)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main
from main import searcharvester_search, searcharvester_extract, searcharvester_extract_page, searcharvester_research, mcp
from orchestrator import Job, JobStatus
from tavily_client import TavilyResult

pytestmark = pytest.mark.asyncio


# ---------- searcharvester_search ----------

async def test_search_returns_normalized_output(monkeypatch):
    results = [
        TavilyResult(url="https://a.com", title="A", content="aaa", score=0.9),
        TavilyResult(url="https://b.com", title="B", content="bbb", score=0.85),
    ]
    monkeypatch.setattr(main, "_execute_search", AsyncMock(return_value=results))

    out = await searcharvester_search(query="test", max_results=2)

    assert out["query"] == "test"
    assert out["result_count"] == 2
    assert out["results"][0] == {"rank": 1, "url": "https://a.com", "title": "A", "content": "aaa"}
    assert out["results"][1]["rank"] == 2
    assert "score" not in out["results"][0]
    assert "follow_up_questions" not in out
    assert "response_time" not in out


async def test_search_advanced_depth_forces_raw_content(monkeypatch):
    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(main, "_execute_search", mock_search)

    await searcharvester_search(query="test", search_depth="advanced", include_raw_content=False)

    assert mock_search.call_args.kwargs["include_raw_content"] is True


async def test_search_upstream_error_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(
        main, "_execute_search",
        AsyncMock(side_effect=HTTPException(status_code=500, detail="upstream down")),
    )

    with pytest.raises(RuntimeError, match="Search failed"):
        await searcharvester_search(query="test")


# ---------- searcharvester_extract ----------

async def test_extract_returns_title_and_truncation(monkeypatch):
    monkeypatch.setattr(
        main, "_extract_markdown_for_url",
        AsyncMock(return_value=("My Title", "x" * 20000)),
    )

    out = await searcharvester_extract(urls=["https://example.com"], extract_depth="basic")

    assert len(out["results"]) == 1
    r = out["results"][0]
    assert r["title"] == "My Title"
    assert r["truncated"] is True
    assert r["chars"] == 10000       # size="m" → SIZE_LIMITS["m"] = 10000
    assert r["total_chars"] == 20000
    assert out["failed"] == []


async def test_extract_failed_url_has_error_reason(monkeypatch):
    monkeypatch.setattr(
        main, "_extract_markdown_for_url",
        AsyncMock(side_effect=HTTPException(status_code=404, detail="Not Found")),
    )

    out = await searcharvester_extract(urls=["https://missing.com"])

    assert out["results"] == []
    assert out["failed"][0] == {"url": "https://missing.com", "error": "HTTP 404: Not Found"}


async def test_extract_all_failed_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(
        main, "_extract_markdown_for_url",
        AsyncMock(side_effect=Exception("connection timeout")),
    )

    with pytest.raises(RuntimeError):
        await searcharvester_extract(urls=["https://a.com", "https://b.com"])


async def test_extract_empty_urls_rejected():
    with pytest.raises(ValidationError):
        await mcp.call_tool("searcharvester_extract", {"urls": []})


# ---------- searcharvester_research ----------

async def test_research_orchestrator_none_raises(monkeypatch):
    monkeypatch.setattr(main, "orchestrator", None)

    with pytest.raises(RuntimeError, match="Research unavailable"):
        await searcharvester_research(query="test")


async def test_research_completes_and_returns_report(monkeypatch):
    mock_orch = MagicMock()
    mock_orch.spawn = AsyncMock(return_value="job-123")
    completed_job = Job(id="job-123", query="test", status=JobStatus.completed, report="# Report")
    mock_orch.get = MagicMock(return_value=completed_job)
    monkeypatch.setattr(main, "orchestrator", mock_orch)

    out = await searcharvester_research(query="test", timeout_sec=60)

    assert out["job_id"] == "job-123"
    assert out["report"] == "# Report"


# ---------- searcharvester_search: engines param ----------

async def test_search_engines_passed_through(monkeypatch):
    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(main, "_execute_search", mock_search)

    await searcharvester_search(query="test", engines="google,brave")

    assert mock_search.call_args.kwargs["engines"] == "google,brave"


async def test_search_engines_defaults_to_none(monkeypatch):
    mock_search = AsyncMock(return_value=[])
    monkeypatch.setattr(main, "_execute_search", mock_search)

    await searcharvester_search(query="test")

    assert mock_search.call_args.kwargs["engines"] is None


# ---------- searcharvester_extract_page ----------

async def test_extract_page_returns_first_page(monkeypatch):
    import time as _time
    extract_id = "abcd1234abcd1234"
    monkeypatch.setitem(
        main._extract_cache,
        extract_id,
        {"url": "https://example.com", "title": "T", "content": "x" * 30000, "created_at": _time.time()},
    )

    out = await searcharvester_extract_page(id=extract_id, page=1)

    assert out["id"] == extract_id
    assert out["chars"] == 25000
    assert out["pages"]["current"] == 1
    assert out["pages"]["total"] == 2


async def test_extract_page_unknown_id_raises():
    with pytest.raises(RuntimeError, match="not found or expired"):
        await searcharvester_extract_page(id="0000000000000000", page=1)


async def test_extract_page_out_of_range_raises(monkeypatch):
    import time as _time
    extract_id = "bbbb1234bbbb1234"
    monkeypatch.setitem(
        main._extract_cache,
        extract_id,
        {"url": "https://example.com", "title": "T", "content": "x" * 100, "created_at": _time.time()},
    )

    with pytest.raises(RuntimeError, match="out of range"):
        await searcharvester_extract_page(id=extract_id, page=99)
