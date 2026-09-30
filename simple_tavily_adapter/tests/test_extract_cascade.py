"""/extract goes through the read cascade and maps its outcome to neutral statuses."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main
import reader


@pytest.fixture
def client(monkeypatch):
    main._extract_cache.clear()
    return TestClient(main.app, base_url="http://localhost", headers={"X-Searcharvester-Client": "1"})


def _fake(result=None, exc=None):
    async def read_page(url, settings, **kw):
        if exc:
            raise exc
        return result
    return read_page


def test_extract_returns_cascade_content(client, monkeypatch):
    res = reader.ReadResult(title="T", content="# Title\n\nbody text", any_completed=True)
    monkeypatch.setattr(reader, "read_page", _fake(res))
    r = client.post("/extract", json={"url": "https://example.com/a"})
    assert r.status_code == 200
    assert "body text" in r.json()["content"]


def test_extract_internal_url_is_400(client, monkeypatch):
    monkeypatch.setattr(reader, "read_page", _fake(exc=reader.UnsafeURL("private")))
    r = client.post("/extract", json={"url": "http://127.0.0.1/"})
    assert r.status_code == 400


def test_extract_empty_page_is_422(client, monkeypatch):
    monkeypatch.setattr(reader, "read_page", _fake(reader.ReadResult(any_completed=True)))
    r = client.post("/extract", json={"url": "https://example.com/empty"})
    assert r.status_code == 422


def test_extract_nothing_answered_is_neutral_502(client, monkeypatch):
    monkeypatch.setattr(reader, "read_page", _fake(reader.ReadResult()))
    r = client.post("/extract", json={"url": "https://example.com/down"})
    assert r.status_code == 502
    detail = r.json()["detail"].lower()
    for word in ("neuraldeep", "playwright", "trafilatura", "proxy"):
        assert word not in detail


def test_extract_page_the_site_does_not_have_is_404(client, monkeypatch):
    """30.09: agents guessed raw GitHub paths; a 502 "temporarily unavailable" made
    them retry the same dead address. A 404 tells them to stop guessing."""
    monkeypatch.setattr(reader, "read_page", _fake(reader.ReadResult(not_found=True)))
    r = client.post("/extract", json={"url": "https://example.com/missing"})
    assert r.status_code == 404
    assert "search results" in r.json()["detail"]
