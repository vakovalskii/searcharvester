"""Extractor choice of the fast path: trafilatura, readability or auto (both, best by the gate).

Why: readability alone lost to trafilatura on pages trafilatura reads (19 vs 23 of 30 in the
2026-09-30 read-log benchmark) but pulled through some pages trafilatura dropped; auto kept
the control group unchanged and cut rejects on hard pages from 33 to 26. The Settings page
picks the mode live (adapter.json), no restart.
"""

import asyncio
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import reader
import search_settings as ss

ARTICLE = "<html><head><title>Big article</title></head><body><article>" + (
    "<p>" + "Retrieval augmented generation combines search with a language model. " * 12 + "</p>") * 8 + \
    "</article></body></html>"


def run_read(monkeypatch, extractor, cands):
    """read_page with extractors replaced by fixed outputs; returns (res, trace, extractor calls)."""
    calls, logged = [], []

    async def fetch(u, proxy=None):
        return reader.Fetched(status=200, html=ARTICLE, final_url=u)

    def fake(name):
        def fn(html, url, fmt="markdown"):
            calls.append(name)
            return cands[name]
        return fn

    async def reader_fn(u, fmt):
        calls.append("paid_reader")
        return {"content": "from the paid reader " * 10}

    async def browser_fn(u, via):
        calls.append("browser")
        return {"content": ""}

    async def judge(*a, **k):
        return (None, "judge_unconfigured", 0)

    async def log(tr):
        logged.append(tr)

    monkeypatch.setattr(reader, "fetch_html", fetch)
    monkeypatch.setattr(reader, "extract", fake("trafilatura"))
    monkeypatch.setattr(reader, "extract_readability", fake("readability"))
    monkeypatch.setattr(reader, "judge", judge)
    st = SimpleNamespace(proxy_url="", extractor=extractor, judge_llm_api_key="")

    async def go():
        r = await reader.read_page("https://example.com/a", st, reader_fn=reader_fn,
                                   browser_fn=browser_fn, log_fn=log)
        await asyncio.sleep(0)
        return r

    res = asyncio.run(go())
    return res, logged[0], calls


LONG = "Clean article text about the topic. " * 250            # ~9 000 chars, gate "ok"
LONGER = LONG + "More of the same article. " * 40
SHORT = "too short"                                          # gate "reject"


def test_auto_takes_the_longer_text_among_accepted(monkeypatch):
    res, tr, calls = run_read(monkeypatch, "auto", {"trafilatura": ("T", LONG), "readability": ("R", LONGER)})
    assert res.path == "fast" and res.content == LONGER and tr.extractor == "readability"
    assert "paid_reader" not in calls


def test_auto_never_prefers_a_rejected_longer_text(monkeypatch):
    junk = "Just a moment... " + "checking your browser before accessing the site. " * 300  # antibot -> reject
    res, tr, _ = run_read(monkeypatch, "auto", {"trafilatura": ("T", LONG), "readability": ("Just a moment...", junk)})
    assert res.content == LONG and tr.extractor == "trafilatura"


def test_auto_rescues_a_page_trafilatura_rejects(monkeypatch):
    res, tr, calls = run_read(monkeypatch, "auto", {"trafilatura": ("T", SHORT), "readability": ("R", LONG)})
    assert res.path == "fast" and tr.extractor == "readability"
    assert "paid_reader" not in calls


def test_auto_does_not_count_link_urls_as_text(monkeypatch):
    linked = " ".join(f"[ref {i}](https://example.com/very/long/path/to/source/{i}?utm=x)" for i in range(150))
    res, tr, _ = run_read(monkeypatch, "auto", {"trafilatura": ("T", LONGER), "readability": ("R", LONG + linked)})
    assert len(LONG + linked) > len(LONGER)     # raw length would pick readability
    assert res.content == LONGER and tr.extractor == "trafilatura"


def test_fixed_extractor_runs_only_itself(monkeypatch):
    for mode in ("trafilatura", "readability"):
        _, tr, calls = run_read(monkeypatch, mode, {"trafilatura": ("T", LONG), "readability": ("R", LONGER)})
        assert tr.extractor == mode and mode in calls
        assert [c for c in calls if c in ("trafilatura", "readability")] == [mode]


def test_trafilatura_mode_keeps_old_behaviour(monkeypatch):
    res, tr, calls = run_read(monkeypatch, "trafilatura", {"trafilatura": ("T", SHORT), "readability": ("R", LONG)})
    assert res.path == "reader" and "paid_reader" in calls   # rejected fast path, as before


def test_unknown_mode_falls_back_to_auto(monkeypatch):
    _, tr, calls = run_read(monkeypatch, "boilerpipe", {"trafilatura": ("T", LONG), "readability": ("R", LONGER)})
    assert set(calls) >= {"trafilatura", "readability"} and tr.extractor == "readability"


def test_readability_extractor_on_real_markup():
    title, text = reader.extract_readability(ARTICLE, "https://example.com/a")
    assert "Retrieval augmented generation" in text and len(text) > 1000
    assert reader.extract_readability("", "https://example.com/x") == ("", "")


def test_settings_file_picks_the_mode(tmp_path, monkeypatch):
    f = tmp_path / "adapter.json"
    monkeypatch.setattr(ss, "PATH", f)
    assert ss.extractor() is None
    for i, (value, expected) in enumerate([("readability", "readability"), ("boilerpipe", None), ("auto", "auto")]):
        f.write_text(json.dumps({"extractor": value}))
        os.utime(f, (1_000_000 + i, 1_000_000 + i))  # the reader caches by mtime; writes within a tick look equal
        assert ss.extractor() == expected


def test_main_applies_the_mode_live(tmp_path, monkeypatch):
    import main
    f = tmp_path / "adapter.json"
    monkeypatch.setattr(ss, "PATH", f)
    main._live_reader_proxy()
    assert main._reader_settings.extractor == "auto"
    f.write_text(json.dumps({"extractor": "trafilatura"}))
    main._live_reader_proxy()
    assert main._reader_settings.extractor == "trafilatura"


def test_old_read_log_gets_the_extractor_column(tmp_path, monkeypatch):
    import read_log
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as c:   # the schema before 2026-09-30
        c.execute("CREATE TABLE page_read_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, "
                  "caller TEXT, url TEXT, host TEXT, path TEXT, fast_status INTEGER, fast_error TEXT, "
                  "fast_via_proxy INTEGER, fast_chars INTEGER, fast_ms INTEGER, gate_decision TEXT, "
                  "gate_reason TEXT, signals TEXT, judge_ok INTEGER, judge_reason TEXT, judge_ms INTEGER, "
                  "reader_called INTEGER, reader_chars INTEGER, reader_ms INTEGER, reader_error TEXT, "
                  "browser_called INTEGER, browser_via_proxy INTEGER, browser_chars INTEGER, browser_ms INTEGER, "
                  "browser_error TEXT, final_chars INTEGER, total_ms INTEGER, snippet TEXT)")
    monkeypatch.setenv("READ_LOG_DB", str(db))
    read_log.init()
    tr = reader.Trace(source="extract", url="https://example.com/a", extractor="readability")
    asyncio.run(read_log.log(tr))
    with sqlite3.connect(db) as c:
        assert c.execute("select extractor from page_read_log").fetchone() == ("readability",)


needs_defuddle = pytest.mark.skipif(not __import__("os").path.exists(reader.DEFUDDLE_WORKER),
                                    reason="defuddle worker is installed in the image only")


@needs_defuddle
def test_defuddle_extractor_on_real_markup():
    title, text = reader.extract_defuddle(ARTICLE, "https://example.com/a")
    assert title == "Big article" and "Retrieval augmented generation" in text
    assert reader.extract_defuddle("", "https://example.com/x") == ("", "")
    # the worker stays up between pages
    proc = reader._defuddle.proc
    reader.extract_defuddle(ARTICLE, "https://example.com/b")
    assert reader._defuddle.proc is proc and proc.poll() is None


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node")
def test_defuddle_hung_worker_is_reset(tmp_path, monkeypatch):
    hang = tmp_path / "hang.mjs"
    hang.write_text("setInterval(() => {}, 1000);\n")        # reads nothing, answers nothing
    w = reader._DefuddleWorker(str(hang))
    monkeypatch.setattr(reader, "DEFUDDLE_TIMEOUT_S", 0.5)
    assert w.call("<p>x</p>", "https://e.com", True) is None
    assert w.proc is None                                     # killed, next call starts fresh


def test_defuddle_missing_worker_is_a_miss(monkeypatch):
    monkeypatch.setattr(reader, "_defuddle", reader._DefuddleWorker("/nonexistent/worker.mjs"))
    assert reader.extract_defuddle(ARTICLE, "https://example.com/a") == ("", "")


def test_auto_considers_defuddle(monkeypatch):
    monkeypatch.setattr(reader, "extract_defuddle", lambda h, u, f="markdown": ("D", LONGER))
    res, tr, calls = run_read(monkeypatch, "auto", {"trafilatura": ("T", SHORT), "readability": ("R", LONG)})
    assert tr.extractor == "defuddle" and res.content == LONGER
