"""search_settings.py and its use in /search and the reader proxy."""
from __future__ import annotations

import json
import os

import main
import search_settings as ss
from tests.test_media import JOB, SEARX_IMAGES, _FakeSearx, api, net  # noqa: F401  (fixtures)


def test_reads_on_the_fly(tmp_path):
    p = tmp_path / "adapter.json"
    assert ss.default_engines("general", p) is None and ss.reader_proxy(p) is None
    p.write_text(json.dumps({"default_engines": {"images": "bing images"}, "reader_proxy": "http://p.example:3128"}))
    assert ss.default_engines("images", p) == "bing images" and ss.reader_proxy(p) == "http://p.example:3128"
    p.write_text("{broken")
    os.utime(p, (1, 1))
    assert ss.default_engines("images", p) is None


def test_search_uses_the_page_engines(api, monkeypatch, tmp_path):  # noqa: F811
    c, _, _ = api
    f = tmp_path / "adapter.json"
    f.write_text(json.dumps({"default_engines": {"images": "bing images", "general": "brave"}}))
    monkeypatch.setattr(ss, "PATH", f)
    fake = _FakeSearx(SEARX_IMAGES)
    monkeypatch.setattr(main.aiohttp, "ClientSession", fake)
    c.post("/search", json={"query": "a", "categories": "images"})
    c.post("/search", json={"query": "b"})
    c.post("/search", json={"query": "c", "engines": "google"})
    assert [s.get("engines") for s in fake.sent] == ["bing images", "brave", "google"]


def test_reader_proxy_overrides_env(monkeypatch, tmp_path):
    f = tmp_path / "adapter.json"
    monkeypatch.setattr(ss, "PATH", f)
    monkeypatch.setattr(main, "_ENV_PROXY", "http://env.example:1")
    assert main._live_reader_proxy() == "http://env.example:1"
    f.write_text(json.dumps({"reader_proxy": "socks5h://page.example:1080"}))
    assert main._live_reader_proxy() == "socks5h://page.example:1080"
    assert main._reader_settings.proxy_url == "socks5h://page.example:1080"
