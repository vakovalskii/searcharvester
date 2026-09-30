"""Images and videos: search hands them out, the job's ledger allows them, /media
fetches them the safe way (media.py, stage D of docs/ui-and-visualization.md)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import media
import reader
from orchestrator import Job

JOB = "0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

SEARX_IMAGES = {"results": [
    {"url": "https://blog.example.com/py313", "title": "Python 3.13 chart", "template": "images.html",
     "img_src": "https://cdn.example.com/chart.png", "thumbnail_src": "https://thumbs.example.net/t.jpg",
     "resolution": "1200 x 800", "engine": "bing images"},
    {"url": "https://commons.example.org/File:x", "title": "no picture fields"},
]}
SEARX_VIDEOS = {"results": [
    {"url": "https://www.youtube.com/watch?v=1kDpOBfYznU", "title": "Python 3.13 release",
     "thumbnail": "https://i.ytimg.com/vi/1kDpOBfYznU/hqdefault.jpg", "length": "525.0", "template": "videos.html"},
    {"url": "https://www.dailymotion.com/video/x9", "title": "3.13", "thumbnail": "//s1.dmcdn.net/v/x", "length": "01:58"},
]}


# ---------- parsing ----------

def test_sniff_accepts_images_only():
    assert media.sniff(PNG) == "png" and media.sniff(JPEG) == "jpg"
    assert media.sniff(b"GIF89a....") == "gif"
    assert media.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert media.sniff(b"\x00\x00\x00\x1cftypavif") == "avif"
    assert media.sniff(b"<svg xmlns=") is None and media.sniff(b"<!doctype html>") is None


def test_items_from_image_and_video_results():
    imgs = media.items_from_results(SEARX_IMAGES["results"], "images")
    assert imgs == [{"kind": "image", "src": "https://cdn.example.com/chart.png", "thumb": "https://thumbs.example.net/t.jpg",
                     "page": "https://blog.example.com/py313", "title": "Python 3.13 chart", "size": "1200 x 800"}]
    vids = media.items_from_results(SEARX_VIDEOS["results"], "videos")
    assert [v["duration"] for v in vids] == ["8:45", "01:58"]
    assert vids[1]["thumb"] == "https://s1.dmcdn.net/v/x"          # protocol-relative made https
    # YouTube: the short stable preview, the engine's one stays allowed as alt
    assert vids[0]["thumb"] == "https://i.ytimg.com/vi/1kDpOBfYznU/hqdefault.jpg"
    assert "alt_thumb" not in vids[0]                              # the engine gave the same one
    brave = media.items_from_results([{"url": "https://youtu.be/1kDpOBfYznU", "title": "t",
                                       "thumbnail": "https://imgs.search.brave.com/long/opaque"}], "videos")[0]
    assert brave["thumb"] == "https://i.ytimg.com/vi/1kDpOBfYznU/hqdefault.jpg"
    assert brave["alt_thumb"] == "https://imgs.search.brave.com/long/opaque"
    assert all(v["kind"] == "video" and v["src"] == v["page"] for v in vids)


def test_ledger_allows_only_what_it_recorded(tmp_path):
    led = media.Ledger(tmp_path)
    items = media.items_from_results(SEARX_IMAGES["results"], "images")
    assert led.record(JOB, items, query="q") == 1
    assert led.record(JOB, items, query="q") == 0                   # no duplicate rows
    assert led.allowed(JOB, "https://cdn.example.com/chart.png")
    assert led.allowed(JOB, "https://thumbs.example.net/t.jpg")      # its preview too
    assert not led.allowed(JOB, "https://evil.example/x.png")
    assert not led.allowed("fedcba9876543210", "https://cdn.example.com/chart.png")  # another job
    assert [i["src"] for i in led.items(JOB)] == ["https://cdn.example.com/chart.png"]
    with pytest.raises(media.MediaError):
        led.allowed("../../etc", "x")


# ---------- fetch ----------

@pytest.fixture
def net(monkeypatch):
    """Every host resolves to a public address unless listed as private; responses by host."""
    routes: dict[str, httpx.Response] = {}
    private = {"internal.example"}

    async def resolve(host):
        return ("private", []) if host in private else ("global", ["93.184.216.34"])

    async def safe(url):
        return (await resolve(httpx.URL(url).host))[0] != "private"

    monkeypatch.setattr(reader, "resolve_addrs", resolve)
    monkeypatch.setattr(reader, "is_safe_url", safe)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        calls.append(host + request.url.raw_path.decode())
        return routes.get(host, httpx.Response(404))

    real = httpx.AsyncClient
    monkeypatch.setattr(media.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler),
                                                                        **{k: v for k, v in kw.items() if k != "proxy"}))
    return routes, calls


def run(coro):
    return asyncio.run(coro)


def test_fetch_png(net):
    routes, _ = net
    routes["cdn.example.com"] = httpx.Response(200, content=PNG, headers={"content-type": "text/html"})
    img = run(media.fetch_image("https://cdn.example.com/chart.png"))
    assert img.ext == "png"                                          # by bytes, not by the header


@pytest.mark.parametrize("resp,status", [
    (httpx.Response(200, content=b"<svg xmlns='http://www.w3.org/2000/svg'/>", headers={"content-type": "image/svg+xml"}), 415),
    (httpx.Response(200, content=b"<html>login</html>", headers={"content-type": "image/png"}), 415),
    (httpx.Response(200, content=PNG + b"\x00" * (6 * 1024 * 1024)), 413),
    (httpx.Response(302, headers={"location": "http://internal.example/secret.png"}), 400),
    (httpx.Response(500), 502),
])
def test_fetch_refusals(net, resp, status):
    routes, _ = net
    routes["cdn.example.com"] = resp
    with pytest.raises(media.MediaError) as e:
        run(media.fetch_image("https://cdn.example.com/x.png"))
    assert e.value.status == status


def test_fetch_internal_address_is_400(net):
    with pytest.raises(media.MediaError) as e:
        run(media.fetch_image("http://internal.example/x.png"))
    assert e.value.status == 400


# ---------- API ----------

@pytest.fixture
def api(monkeypatch, tmp_path, net):
    led = media.Ledger(tmp_path)
    monkeypatch.setattr(main, "media_ledger", led)
    job = Job(id=JOB, query="q")
    orch = MagicMock()
    orch.get = MagicMock(side_effect=lambda j: job if j == JOB else None)
    monkeypatch.setattr(main, "orchestrator", orch)
    return TestClient(main.app, base_url="http://localhost", headers={"X-Searcharvester-Client": "1"}), led, net


def test_media_endpoint(api):
    c, led, (routes, calls) = api
    led.record(JOB, media.items_from_results(SEARX_IMAGES["results"], "images"))
    routes["cdn.example.com"] = httpx.Response(200, content=PNG)
    src = "https://cdn.example.com/chart.png"
    r = c.get("/media", params={"job": JOB, "src": src})
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and "default-src 'none'" in r.headers["content-security-policy"]
    n = len(calls)
    assert c.get("/media", params={"job": JOB, "src": src}).content == PNG
    assert len(calls) == n                                             # second time from the cache
    assert c.get("/media", params={"job": JOB, "src": "https://cdn.example.com/other.png"}).status_code == 404
    assert c.get("/media", params={"job": "nope", "src": src}).status_code == 422
    assert c.get(f"/research/{JOB}/media").json()["items"][0]["src"] == src


class _FakeSearx:
    """aiohttp.ClientSession stand-in: records the form it got, answers `data`."""
    def __init__(self, data):
        self.data, self.sent = data, []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, data=None, **k):
        self.sent.append(dict(data or {}))
        outer = self

        class _R:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def json(self): return outer.data
        return _R()


def test_image_search_uses_image_engines_and_fills_the_ledger(api, monkeypatch):
    c, led, _ = api
    fake = _FakeSearx(SEARX_IMAGES)
    monkeypatch.setattr(main.aiohttp, "ClientSession", fake)
    r = c.post("/search", json={"query": "python chart", "categories": "images"}, headers={"X-Searcharvester-Job": JOB})
    body = r.json()
    assert "engines" not in fake.sent[0] and fake.sent[0]["categories"] == "images"
    assert body["images"] == ["https://cdn.example.com/chart.png"]
    assert body["results"][0]["img_src"] == "https://cdn.example.com/chart.png"
    assert "img_src" not in body["results"][1]                         # no null media fields
    assert led.allowed(JOB, "https://cdn.example.com/chart.png")
    # the same words as a web search are not a duplicate of the image search
    r2 = c.post("/search", json={"query": "python chart"}, headers={"X-Searcharvester-Job": JOB})
    assert "notice" not in r2.json() and fake.sent[1]["engines"] == "google,duckduckgo,brave"


def test_search_without_a_job_writes_no_ledger(api, monkeypatch):
    c, led, _ = api
    monkeypatch.setattr(main.aiohttp, "ClientSession", _FakeSearx(SEARX_VIDEOS))
    body = c.post("/search", json={"query": "py", "categories": "videos"}).json()
    assert body["results"][0]["duration"] == "8:45" and body["results"][0]["thumbnail"].startswith("https://i.ytimg")
    assert led.items(JOB) == []


def test_a_retyped_url_finds_its_entry_and_fetches_the_ledger_one(tmp_path):
    """30.09 live run: the model dropped a slash inside a base64 path segment of a
    brave preview; the picture must still show, and only the ledger URL is fetched."""
    led = media.Ledger(tmp_path)
    brave = "https://imgs.search.brave.com/Em/rs:fit:200:200:1:0/g:ce/aHR0cHM6Ly9pLnl0/aW1nLmNvbS92aS8x/LmpwZw"
    led.record(JOB, [{"kind": "video", "src": "https://www.dailymotion.com/video/x9", "thumb": brave,
                      "page": "https://www.dailymotion.com/video/x9", "title": "t"}])
    typo = brave.replace("Ly9pLnl0/aW1n", "Ly9pLnl0aW1n")
    assert led.resolve(JOB, typo) == brave
    assert led.resolve(JOB, brave) == brave
    assert led.resolve(JOB, "https://imgs.search.brave.com/Other") is None


def test_media_endpoint_fetches_the_ledger_url_for_a_retyped_src(api):
    c, led, (routes, calls) = api
    good = "https://cdn.example.com/a/b/chart.png"
    led.record(JOB, [{"kind": "image", "src": good, "thumb": None, "page": "https://blog.example.com/p", "title": "t"}])
    routes["cdn.example.com"] = httpx.Response(200, content=PNG)
    r = c.get("/media", params={"job": JOB, "src": "https://cdn.example.com/ab/chart.png"})
    assert r.status_code == 200 and calls[-1] == "cdn.example.com/a/b/chart.png"
