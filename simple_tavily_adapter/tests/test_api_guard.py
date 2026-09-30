"""Stage 0 of docs/ui-and-visualization.md: the unauthenticated localhost API
must not be drivable by a foreign web page (plain form POST, DNS rebinding)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import main

OK = {"X-Searcharvester-Client": "1"}


@pytest.fixture
def orch(monkeypatch):
    o = MagicMock()
    o.spawn = AsyncMock(return_value="abcdef0123456789")
    o.cancel = AsyncMock(return_value=True)
    o.get = MagicMock(return_value=None)
    monkeypatch.setattr(main, "orchestrator", o)
    return o


def client(host="localhost"):
    return TestClient(main.app, base_url=f"http://{host}")


def test_post_without_client_header_is_refused(orch):
    r = client().post("/research", json={"query": "x"})
    assert r.status_code == 403
    orch.spawn.assert_not_called()


@pytest.mark.parametrize("ctype,body", [
    ("application/x-www-form-urlencoded", "query=x"),
    ("text/plain", '{"query": "x"}'),
    ("multipart/form-data; boundary=b", "--b\r\nContent-Disposition: form-data; name=query\r\n\r\nx\r\n--b--"),
])
def test_simple_form_posts_are_refused(orch, ctype, body):
    """What an HTML form on any site can send without a preflight."""
    r = client().post("/research", content=body, headers={"content-type": ctype})
    assert r.status_code == 403
    orch.spawn.assert_not_called()


def test_foreign_origin_is_refused_even_with_the_header(orch):
    r = client().post("/research", json={"query": "x"}, headers={**OK, "Origin": "https://evil.example"})
    assert r.status_code == 403
    orch.spawn.assert_not_called()


def test_our_origin_with_the_header_is_accepted(orch):
    r = client().post("/research", json={"query": "x"}, headers={**OK, "Origin": "http://localhost:9762"})
    assert r.status_code == 202


def test_delete_needs_the_header_too(orch):
    assert client().delete("/research/abcdef0123456789").status_code == 403
    orch.cancel.assert_not_called()


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8010", "127.0.0.1.evil.example"])
def test_foreign_host_is_refused_on_get(orch, host):
    """DNS rebinding: a foreign name pointing at 127.0.0.1 reads our GETs otherwise."""
    assert client(host).get("/health").status_code == 400
    assert client(host).get("/research/abcdef0123456789").status_code == 400


@pytest.mark.parametrize("host", ["localhost", "localhost:8010", "127.0.0.1:8000", "tavily-adapter:8000"])
def test_our_hosts_pass(orch, host):
    assert client(host).get("/health").status_code == 200


@pytest.mark.parametrize("host,ok", [
    ("[::1]:8010", True), ("[::1]", True), ("LOCALHOST:9", True),
    ("[::2]:8010", False), ("localhost.evil.example", False), ("", False), ("a:b:c", False),
])
def test_host_parser(host, ok):
    assert main._host_allowed(host) is ok


def test_preflight_from_our_ui_passes():
    r = client().options("/research", headers={
        "Origin": "http://localhost:9762", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-searcharvester-client"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:9762"


def test_preflight_from_a_foreign_origin_is_not_allowed():
    r = client().options("/research", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-searcharvester-client"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.example"


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc", "abc", "ABCDEF0123456789", "abcdef0123456789a", "zzzzzzzzzzzzzzzz"])
@pytest.mark.parametrize("route", ["", "/events", "/snapshot", "/logs"])
def test_bad_job_ids_are_422_everywhere(orch, bad, route):
    r = client().get(f"/research/{bad}{route}")
    assert r.status_code in (404, 422), r.status_code
    assert r.status_code == 422 or "%2F" in bad  # an encoded slash never reaches the route


def test_bad_job_header_is_ignored_not_trusted(orch):
    orch.get.return_value = None
    assert main._job_guard("../../etc/passwd") is None
    assert main._job_guard("ABCDEF0123456789") is None
    orch.get.assert_not_called()


def test_skill_scripts_and_benches_send_the_header():
    from pathlib import Path
    import os
    repo = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[2]))
    files = list((repo / "hermes_skills").glob("*/scripts/*.py")) + list((repo / "bench").glob("*.py"))
    if not files:
        pytest.skip("repo not mounted")
    for f in files:
        text = f.read_text()
        if "urllib.request.Request(" in text and "data=" in text:
            assert "searcharvester-client" in text.lower(), f
