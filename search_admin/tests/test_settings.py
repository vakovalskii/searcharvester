"""settings.py: validation, secrets, rendering, files."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import settings as st  # noqa: E402

BASE = {
    "use_default_settings": True,
    "server": {"secret_key": "k", "limiter": False},
    "outgoing": {"request_timeout": 10.0},
    "engines": [{"name": "google", "disabled": False}, {"name": "bing", "disabled": True}],
    "adapter": {"searxng_url": "http://searxng:8080"},
}
NAMES = {"google", "bing", "brave", "bing images", "youtube"}


def test_mask_and_keep_secret():
    u = "socks5h://bob:s3cret@proxy.example:1080"
    assert st.mask_proxy(u) == "socks5h://bob:***@proxy.example:1080"
    assert st.mask_proxy("http://p.example:3128") == "http://p.example:3128"
    assert st.keep_secret("socks5h://bob:***@proxy.example:1080", [u]) == u
    assert st.keep_secret("http://new.example:3128", [u]) == "http://new.example:3128"


@pytest.mark.parametrize("url,ok", [
    ("http://p.example:3128", True), ("socks5h://u:p@10.0.0.1:1080", True),
    ("ftp://p.example:21", False), ("http://p.example", False), ("http://p.example:3128/x", False), ("nonsense", False),
])
def test_check_proxy(url, ok):
    assert (st.check_proxy(url) is None) == ok


def test_normalize_valid_body():
    body = {"searxng": {"engines": {"bing": True, "google": False}, "proxies": ["http://p.example:3128"],
                        "request_timeout": 8, "max_request_timeout": 20},
            "adapter": {"default_engines": {"general": ["google", "brave"], "images": []},
                        "reader_proxy": "socks5h://u:pw@fi.example:1080"}}
    ov, errors = st.normalize(body, st.empty(), NAMES)
    assert errors == []
    assert ov["searxng"]["engines"] == {"bing": True, "google": False}
    assert ov["adapter"]["default_engines"] == {"general": ["google", "brave"]}   # empty = category's own
    assert st.masked(ov)["adapter"]["reader_proxy"] == "socks5h://u:***@fi.example:1080"


def test_masked_value_sent_back_keeps_the_stored_secret():
    stored, _ = st.normalize({"searxng": {"proxies": ["http://u:pw@p.example:3128"]}}, st.empty(), NAMES)
    ov, errors = st.normalize({"searxng": {"proxies": ["http://u:***@p.example:3128"]}}, stored, NAMES)
    assert errors == [] and ov["searxng"]["proxies"] == ["http://u:pw@p.example:3128"]


@pytest.mark.parametrize("body,needle", [
    ({"searxng": {"engines": {"nope": True}}}, "unknown engine"),
    ({"searxng": {"engines": {"bing": "yes"}}}, "true or false"),
    ({"searxng": {"proxies": ["ftp://x:1"]}}, "scheme"),
    ({"searxng": {"request_timeout": 500}}, "request_timeout"),
    ({"searxng": {"request_timeout": 10, "max_request_timeout": 5}}, "must not be below"),
    ({"adapter": {"default_engines": {"weird": ["google"]}}}, "unknown category"),
    ({"adapter": {"default_engines": {"general": ["google; rm -rf /"]}}}, "engine names"),
    ({"adapter": {"default_engines": {"images": ["yandex images"]}}}, "unknown engine"),
    ({"adapter": {"reader_proxy": "http://nohost"}}, "port"),
    ([], "object"),
])
def test_normalize_errors(body, needle):
    _, errors = st.normalize(body, st.empty(), NAMES)
    assert errors and any(needle in e for e in errors), errors


def test_render_searxng():
    ov, _ = st.normalize({"searxng": {"engines": {"bing": True, "youtube": False}, "proxies": ["http://p.example:3128"],
                                      "request_timeout": 8}}, st.empty(), NAMES)
    s = st.render_searxng(BASE, ov)
    assert "adapter" not in s and s["server"]["secret_key"] == "k"
    assert s["outgoing"] == {"request_timeout": 8.0, "proxies": {"all://": ["http://p.example:3128"]}}
    eng = {e["name"]: e["disabled"] for e in s["engines"]}
    assert eng == {"google": False, "bing": False, "youtube": True}
    assert BASE["engines"][1]["disabled"] is True        # base not mutated


def test_render_without_overrides_is_the_base():
    s = st.render_searxng(BASE, st.empty())
    assert s == {k: v for k, v in BASE.items() if k != "adapter"}


def test_store_roundtrip(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(BASE))
    store = st.Store(tmp_path / "config.yaml", tmp_path / "out")
    store.render()
    assert yaml.safe_load(store.searxng_path.read_text())["server"]["secret_key"] == "k"
    ov, _ = st.normalize({"adapter": {"default_engines": {"images": ["bing images"]}}}, store.overrides(), NAMES)
    store.save(ov)
    assert json.loads(store.adapter_path.read_text()) == {"default_engines": {"images": "bing images"}, "reader_proxy": None, "extractor": None}
    assert store.overrides()["adapter"]["default_engines"] == {"images": ["bing images"]}
    assert oct(store.overrides_path.stat().st_mode & 0o777) == "0o600"


def test_extractor_choice_is_validated_and_rendered():
    ov, errors = st.normalize({"adapter": {"extractor": "readability"}}, st.empty(), NAMES)
    assert not errors and ov["adapter"]["extractor"] == "readability"
    assert st.render_adapter(ov)["extractor"] == "readability"
    ov, errors = st.normalize({"adapter": {"extractor": "boilerpipe"}}, ov, NAMES)
    assert errors and "extractor" in errors[0] and ov["adapter"]["extractor"] == "readability"
    ov, errors = st.normalize({"adapter": {"extractor": None}}, ov, NAMES)
    assert not errors and st.render_adapter(ov)["extractor"] is None
