"""FastAPI route tests with a mocked orchestrator (no Docker required)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import main
from orchestrator import Job, JobStatus


@pytest.fixture
def client(monkeypatch):
    mock_orch = MagicMock()
    mock_orch.spawn = AsyncMock(return_value="abcdef0123456789")
    mock_orch.cancel = AsyncMock(return_value=True)
    mock_orch.get = MagicMock(return_value=None)
    mock_orch.read_logs = MagicMock(return_value=None)
    mock_orch.load_meta = MagicMock(return_value=None)
    monkeypatch.setattr(main, "orchestrator", mock_orch)
    return TestClient(main.app, base_url="http://localhost", headers={"X-Searcharvester-Client": "1"}), mock_orch


def test_post_research_empty_query_returns_422(client):
    c, _ = client
    r = c.post("/research", json={"query": ""})
    assert r.status_code == 422


def test_post_research_missing_query_returns_422(client):
    c, _ = client
    r = c.post("/research", json={})
    assert r.status_code == 422


def test_post_research_returns_202_and_job_id(client):
    c, orch = client
    r = c.post("/research", json={"query": "what is RAG"})
    assert r.status_code == 202
    assert r.json() == {"job_id": "abcdef0123456789", "status": "queued"}
    orch.spawn.assert_awaited_once_with(query="what is RAG", depth="deep")


def test_post_research_quick_depth_is_passed(client):
    c, orch = client
    r = c.post("/research", json={"query": "who won", "depth": "quick"})
    assert r.status_code == 202
    orch.spawn.assert_awaited_once_with(query="who won", depth="quick")


def test_post_research_unknown_depth_is_422(client):
    c, _ = client
    assert c.post("/research", json={"query": "x", "depth": "huge"}).status_code == 422


def test_get_research_unknown_returns_404(client):
    c, orch = client
    orch.get.return_value = None
    r = c.get("/research/deadbeefdeadbeef")
    assert r.status_code == 404


def test_get_research_running_does_not_include_report(client):
    c, orch = client
    orch.get.return_value = Job(id="0123456789abcdef", query="x", status=JobStatus.running)
    r = c.get("/research/0123456789abcdef")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body.get("report") is None


def test_get_research_completed_returns_report_content(client):
    c, orch = client
    orch.get.return_value = Job(
        id="0123456789abcdef",
        query="x",
        status=JobStatus.completed,
        report="# Title\n\n[1] src\n",
    )
    r = c.get("/research/0123456789abcdef")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["report"].startswith("# Title")


def test_delete_research_calls_cancel(client):
    c, orch = client
    orch.get.return_value = Job(id="0123456789abcdef", query="x", status=JobStatus.running)
    r = c.delete("/research/0123456789abcdef")
    assert r.status_code == 200
    orch.cancel.assert_awaited_once_with("0123456789abcdef")
