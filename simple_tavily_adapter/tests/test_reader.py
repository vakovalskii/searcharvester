"""reader.py (ported from neuraldeep search-api): safety, proxy routing, quality gate, judge and cascade.

Network and DNS are mocked. The live benchmark on real sites lives in
bench/read_bench.py and is run by hand before and after cascade changes.
"""

import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest

import reader

ARTICLE = "<html><head><title>Big article</title></head><body><article>" + (
    "<p>" + "Retrieval augmented generation combines search with a language model. " * 12 + "</p>") * 8 + \
    "</article></body></html>"


def settings(**over):
    base = dict(proxy_url="", judge_llm_url="http://judge", judge_llm_api_key="k",
                judge_llm_model="m", judge_timeout_s=1.0)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def blocked(self, *a, **k):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)


def fake_dns(monkeypatch, table):
    async def getaddrinfo(self, host, port, **kw):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]

    monkeypatch.setattr(asyncio.AbstractEventLoop, "getaddrinfo", getaddrinfo, raising=False)
    monkeypatch.setattr(type(asyncio.new_event_loop()), "getaddrinfo", getaddrinfo)


# ── safety ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a", True),
    ("http://billing-api:8300/x", False),      # docker service name -> private address
    ("http://postgres:5432", False),
    ("http://evil.test/", False),              # public-looking name resolving inside
    ("http://127.0.0.1/", False),
    ("http://[::1]/", False),
    ("ftp://example.com/", False),
    ("file:///etc/passwd", False),
    ("https://unknown.invalid/", True),        # our DNS does not know it: not SSRF, reader may open it
])
def test_is_safe_url(monkeypatch, url, ok):
    fake_dns(monkeypatch, {"example.com": "93.184.216.34", "billing-api": "172.20.0.5",
                           "postgres": "172.20.0.2", "evil.test": "10.0.0.7"})
    assert asyncio.run(reader.is_safe_url(url)) is ok


def test_fetch_refuses_redirect_into_internal_network(monkeypatch):
    fake_dns(monkeypatch, {"example.com": "93.184.216.34", "billing-api": "172.20.0.5"})

    async def send(self, request, **kw):
        return httpx.Response(302, headers={"location": "http://billing-api:8300/admin"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    f = asyncio.run(reader.fetch_html("https://example.com/go"))
    assert f.error == "unsafe_url" and f.final_url == "http://billing-api:8300/admin"


def test_fetch_marks_unresolvable_host(monkeypatch):
    fake_dns(monkeypatch, {})
    f = asyncio.run(reader.fetch_html("https://pmc.ncbi.nlm.nih.gov/articles/PMC1"))
    assert f.error == "dns_unresolved"


def test_fetch_reads_html(monkeypatch):
    fake_dns(monkeypatch, {"example.com": "93.184.216.34"})

    async def send(self, request, **kw):
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              content=ARTICLE.encode(), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    f = asyncio.run(reader.fetch_html("https://example.com/a"))
    assert f.status == 200 and "Big article" in f.html


def test_fetch_connects_to_the_checked_address(monkeypatch):
    """DNS rebinding: the name must not be resolved a second time at connect."""
    fake_dns(monkeypatch, {"example.com": "93.184.216.34"})
    seen = {}

    async def send(self, request, **kw):
        seen.update(url=str(request.url), host=request.headers.get("host"),
                    sni=request.extensions.get("sni_hostname"))
        return httpx.Response(200, headers={"content-type": "text/html"}, content=ARTICLE.encode(), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    f = asyncio.run(reader.fetch_html("https://example.com:8443/a?b=1"))
    assert f.status == 200 and f.final_url == "https://example.com:8443/a?b=1"
    assert seen == {"url": "https://93.184.216.34:8443/a?b=1", "host": "example.com:8443", "sni": "example.com"}


def test_pinned_ipv6_and_plain_http():
    url, headers, ext = reader._pinned("http://example.com/x", "2606:2800:220:1::1")
    assert url == "http://[2606:2800:220:1::1]/x" and headers == {"Host": "example.com"} and ext == {}


def test_fetch_refuses_name_that_rebinds_to_internal(monkeypatch):
    """First answer public, second internal: the second lookup is the one that counts."""
    answers = iter(["93.184.216.34", "127.0.0.1"])

    async def getaddrinfo(self, host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), 0))]

    monkeypatch.setattr(type(asyncio.new_event_loop()), "getaddrinfo", getaddrinfo)

    async def send(self, request, **kw):
        raise AssertionError("must not connect")

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    f = asyncio.run(reader.fetch_html("http://rebind.test/"))
    assert f.error == "unsafe_url"


# ── proxy routing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,status,error,expected", [
    ("https://openrouter.ai/models", 403, None, True),
    ("https://openrouter.ai/models", None, "timeout", True),
    ("https://openrouter.ai/models", 404, None, False),
    ("https://openrouter.ai/models", 200, None, False),
    ("https://www.rbc.ru/", 403, None, False),         # Russian site: never via proxy
    ("https://кто.рф/", None, "timeout", False),
    ("https://xn--h1ahn.xn--p1ai/", 403, None, False),
])
def test_should_retry_via_proxy(url, status, error, expected):
    assert reader.should_retry_via_proxy(url, status, error, proxy_configured=True) is expected


def test_no_proxy_configured_never_retries():
    assert reader.should_retry_via_proxy("https://openrouter.ai", 403, None, proxy_configured=False) is False


# ── quality gate ───────────────────────────────────────────────────────────

def sig(**over):
    base = {"html_len": 50000, "text_len": 6000, "visible_ratio": 0.3, "scripts": 5,
            "empty_root": False, "antibot": False, "links_per_1k": 1.0}
    base.update(over)
    return base


@pytest.mark.parametrize("over,decision,reason", [
    ({}, "ok", "long_clean_text"),
    ({"antibot": True}, "reject", "antibot"),
    ({"text_len": 120}, "reject", "too_short"),
    ({"empty_root": True, "text_len": 900}, "reject", "spa_shell"),
    ({"visible_ratio": 0.01, "scripts": 60, "text_len": 900}, "reject", "spa_shell"),
    ({"text_len": 1800}, "judge", "gray_zone"),
    ({"text_len": 5000}, "judge", "gray_zone"),       # a 4-5k intro of a long article went through at 4000
    ({"links_per_1k": 20.0}, "judge", "gray_zone"),   # long but a link farm: ask
])
def test_gate(over, decision, reason):
    assert reader.gate(sig(**over)) == (decision, reason)


def test_signals_detect_spa_shell_and_antibot():
    shell = "<html><head><title>App</title></head><body><div id=\"root\"></div>" + \
            "<script src=a.js></script>" * 30 + "</body></html>"
    s = reader.signals(shell, "", "App")
    assert s["empty_root"] and s["scripts"] == 30
    bot = reader.signals("<html><title>Just a moment...</title></html>", "", "Just a moment...")
    assert bot["antibot"]


# ── judge ──────────────────────────────────────────────────────────────────

def judge_reply(monkeypatch, content=None, exc=None):
    async def send(self, request, **kw):
        if exc:
            raise exc
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)


def test_judge_parses_verdict(monkeypatch):
    judge_reply(monkeypatch, '{"ok": false, "reason": "listing_cut"}')
    ok, reason, _ = asyncio.run(reader.judge("u", "t", "text", sig(), settings()))
    assert ok is False and reason == "listing_cut"


@pytest.mark.parametrize("content", ["not json", '{"ok": "yes"}', ""])
def test_judge_garbage_is_none(monkeypatch, content):
    judge_reply(monkeypatch, content)
    ok, reason, _ = asyncio.run(reader.judge("u", "t", "text", sig(), settings()))
    assert ok is None and reason == "judge_garbage"


def test_judge_down_is_none(monkeypatch):
    judge_reply(monkeypatch, exc=httpx.ConnectError("down"))
    ok, reason, _ = asyncio.run(reader.judge("u", "t", "text", sig(), settings()))
    assert ok is None and reason.startswith("judge_error")


def test_judge_unconfigured(monkeypatch):
    ok, reason, _ = asyncio.run(reader.judge("u", "t", "x", sig(), settings(judge_llm_api_key="")))
    assert ok is None and reason == "judge_unconfigured"


# ── cascade ────────────────────────────────────────────────────────────────

class Calls(list):
    pass


def cascade(monkeypatch, *, fetched, gate_result=None, judge_result=(None, "judge_error", 0),
            reader_out=None, browser_out=None, proxy="", url="https://example.com/a", min_provider=1):
    calls = Calls()
    monkeypatch.setattr(reader, "MIN_PROVIDER_TEXT", min_provider)
    logged = []

    async def fetch(u, proxy=None):
        calls.append(("fetch", bool(proxy)))
        f = fetched(u, proxy) if callable(fetched) else fetched
        return f

    async def judge(*a, **k):
        calls.append(("judge",))
        return judge_result

    async def reader_fn(u, fmt):
        calls.append(("reader",))
        if isinstance(reader_out, Exception):
            raise reader_out
        return reader_out or {"content": ""}

    async def browser_fn(u, via_proxy):
        calls.append(("browser", via_proxy))
        if isinstance(browser_out, Exception):
            raise browser_out
        return browser_out or {"content": ""}

    async def log(tr):
        logged.append(tr)

    monkeypatch.setattr(reader, "fetch_html", fetch)
    monkeypatch.setattr(reader, "judge", judge)
    if gate_result:
        monkeypatch.setattr(reader, "gate", lambda s: gate_result)

    async def run():
        res = await reader.read_page(url, settings(proxy_url=proxy), reader_fn=reader_fn,
                                     browser_fn=browser_fn, log_fn=log, source="read")
        await asyncio.sleep(0)
        return res

    res = asyncio.run(run())
    return res, calls, logged


def ok_html(u, p=None):
    return reader.Fetched(status=200, html=ARTICLE, final_url=u)


def test_fast_path_accepted_skips_paid_reader(monkeypatch):
    res, calls, logged = cascade(monkeypatch, fetched=ok_html, gate_result=("ok", "long_clean_text"))
    assert res.path == "fast" and "Retrieval" in res.content
    assert ("reader",) not in calls
    assert logged[0].path == "fast" and logged[0].reader_called is False


def test_gray_zone_judge_ok_accepts_fast(monkeypatch):
    res, calls, _ = cascade(monkeypatch, fetched=ok_html, gate_result=("judge", "gray_zone"),
                            judge_result=(True, "article", 300))
    assert res.path == "fast" and ("reader",) not in calls


def test_gray_zone_judge_no_goes_to_reader(monkeypatch):
    res, calls, logged = cascade(monkeypatch, fetched=ok_html, gate_result=("judge", "gray_zone"),
                                 judge_result=(False, "listing_cut", 300),
                                 reader_out={"title": "R", "content": "full listing"})
    assert res.path == "reader" and res.content == "full listing"
    assert logged[0].judge_ok is False and logged[0].judge_reason == "listing_cut"


def test_judge_unavailable_prefers_quality(monkeypatch):
    res, calls, _ = cascade(monkeypatch, fetched=ok_html, gate_result=("judge", "gray_zone"),
                            judge_result=(None, "judge_error:ConnectError", 10),
                            reader_out={"content": "from reader"})
    assert res.path == "reader"


def test_rejected_fast_path_goes_to_reader_then_browser(monkeypatch):
    res, calls, logged = cascade(monkeypatch, fetched=ok_html, gate_result=("reject", "spa_shell"),
                                 reader_out={"content": ""}, browser_out={"title": "B", "content": "rendered"})
    assert res.path == "browser" and res.content == "rendered"
    assert [c[0] for c in calls] == ["fetch", "reader", "browser"]
    assert logged[0].gate_reason == "spa_shell"


def test_foreign_refusal_retries_fast_path_via_proxy(monkeypatch):
    def fetched(u, proxy):
        return ok_html(u) if proxy else reader.Fetched(status=403, final_url=u)

    res, calls, logged = cascade(monkeypatch, fetched=fetched, gate_result=("ok", "long_clean_text"),
                                 proxy="http://px", url="https://openrouter.ai/models")
    assert res.path == "fast" and calls[:2] == [("fetch", False), ("fetch", True)]
    assert logged[0].fast_via_proxy is True


def test_russian_refusal_never_uses_proxy(monkeypatch):
    res, calls, _ = cascade(monkeypatch, fetched=reader.Fetched(status=403), proxy="http://px",
                            url="https://www.ozon.ru/x", reader_out={"content": "ozon via reader"})
    assert res.path == "reader"
    assert ("fetch", True) not in calls
    assert ("browser", True) not in calls


def test_browser_retries_via_proxy_for_foreign_site(monkeypatch):
    def browser_calls(monkeypatch):
        pass

    res, calls, _ = cascade(monkeypatch, fetched=reader.Fetched(error="timeout"), proxy="http://px",
                            url="https://openrouter.ai/models", reader_out=RuntimeError("down"),
                            browser_out={"content": ""})
    assert ("browser", False) in calls and ("browser", True) in calls
    assert res.path == "" and res.any_completed is True


def test_unresolvable_host_goes_straight_to_reader(monkeypatch):
    res, calls, _ = cascade(monkeypatch, fetched=reader.Fetched(error="dns_unresolved"), proxy="http://px",
                            url="https://pmc.ncbi.nlm.nih.gov/articles/PMC1",
                            reader_out={"content": "article text " * 20})
    assert res.path == "reader"
    assert ("fetch", True) not in calls and not any(c[0] == "browser" for c in calls)


def test_tiny_provider_answer_is_not_content(monkeypatch):
    res, calls, _ = cascade(monkeypatch, fetched=reader.Fetched(status=498), reader_out={"content": "x" * 17},
                            browser_out={"content": "y" * 40}, url="https://www.wildberries.ru/x",
                            min_provider=100)
    assert res.path == "" and res.any_completed is True


def test_everything_down(monkeypatch):
    res, calls, logged = cascade(monkeypatch, fetched=reader.Fetched(error="timeout"),
                                 reader_out=RuntimeError("down"), browser_out=RuntimeError("down"))
    assert res.path == "" and res.any_completed is False
    assert logged[0].reader_error and logged[0].browser_error


def test_unsafe_url_raises_and_calls_nothing_paid(monkeypatch):
    with pytest.raises(reader.UnsafeURL):
        cascade(monkeypatch, fetched=reader.Fetched(error="unsafe_url"), reader_out={"content": "x"})


def test_read_log_writes_every_trace_field(tmp_path, monkeypatch):
    """Every Trace field is a sqlite column: a new field without a column breaks the log."""
    import sqlite3
    from dataclasses import fields

    import read_log
    monkeypatch.setenv("READ_LOG_DB", str(tmp_path / "log.sqlite3"))
    read_log.init()
    tr = reader.Trace(source="extract", url="https://example.com/a")
    tr.signals = {"chars": 10}
    asyncio.run(read_log.log(tr))
    with sqlite3.connect(tmp_path / "log.sqlite3") as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(page_read_log)")}
        host, signals = db.execute("select host, signals from page_read_log").fetchone()
    assert {f.name for f in fields(reader.Trace)} <= cols
    assert host == "example.com" and '"chars": 10' in signals


def test_fetch_through_proxy_keeps_the_name(monkeypatch):
    """httpcore ignores SNI in a proxy tunnel; the proxy resolves and filters itself."""
    fake_dns(monkeypatch, {"example.com": "93.184.216.34"})
    seen = {}

    async def send(self, request, **kw):
        seen["url"] = str(request.url)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=ARTICLE.encode(), request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    f = asyncio.run(reader.fetch_html("https://example.com/a", proxy="http://u:p@proxy.test:3128"))
    assert f.status == 200 and seen["url"] == "https://example.com/a"


@pytest.mark.parametrize("url,bot", [
    ("https://en.wikipedia.org/wiki/RAG", True),
    ("https://upload.wikimedia.org/x", True),
    ("https://wikipedia.org/", True),
    ("https://notwikipedia.org/", False),
    ("https://example.com/", False),
])
def test_honest_user_agent_for_wikimedia(url, bot):
    assert (reader.user_agent_for(url) == reader.BOT_UA) is bot


@pytest.mark.parametrize("status", [404, 410])
def test_page_the_origin_does_not_have_skips_reader_and_browser(monkeypatch, status):
    res, calls, _ = cascade(monkeypatch, fetched=reader.Fetched(status=status),
                            reader_out={"content": "should not be asked " * 20})
    assert res.not_found and not res.content
    assert ("reader",) not in calls and not any(c[0] == "browser" for c in calls)


def test_other_4xx_still_tries_the_cascade(monkeypatch):
    res, calls, _ = cascade(monkeypatch, fetched=reader.Fetched(status=403),
                            reader_out={"content": "article text " * 20})
    assert res.path == "reader" and not res.not_found
