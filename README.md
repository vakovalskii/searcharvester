# Searcharvester 🌾

**Self-hosted search + main-content harvester for AI agents**

> 📖 **Docs:** [English](docs/en/README.md) · [Русский](docs/ru/README.md) · [中文](docs/zh/README.md)

SearXNG (100+ search engines) + FastAPI adapter + trafilatura (markdown extraction). Tavily-compatible API, no keys, no quotas. Pre-built image on GHCR — `docker compose up -d` and go.

> 🎯 **One `docker compose up -d`** — local search + extract API for your LLM pipelines.

## 🚀 Quick start

```bash
# 1. Clone
git clone git@github.com:vakovalskii/searcharvester.git
# or HTTPS: git clone https://github.com/vakovalskii/searcharvester.git
cd searcharvester

# 2. Config
cp config.example.yaml config.yaml
# Change server.secret_key in config.yaml (32+ chars)

# 3. Start (pulls pre-built image from ghcr.io/vakovalskii/searcharvester)
docker compose up -d

# 4. Test search
curl -X POST "http://localhost:8000/search" \
     -H "Content-Type: application/json" \
     -d '{"query": "bitcoin price", "max_results": 3}'

# 5. Test markdown extraction
curl -X POST "http://localhost:8000/extract" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://en.wikipedia.org/wiki/Docker_(software)", "size": "m"}'
```

## 💡 Usage

### Drop-in replacement for Tavily

```python
# Keep using the official Tavily client
pip install tavily-python

from tavily import TavilyClient

# Just change base_url!
client = TavilyClient(
    api_key="whatever",  # ignored
    base_url="http://localhost:8000"  # your adapter
)

response = client.search(
    query="bitcoin price",
    max_results=5,
    include_raw_content=True
)
```

### Plain HTTP

```python
import requests

response = requests.post("http://localhost:8000/search", json={
    "query": "what is machine learning",
    "max_results": 5,
    "include_raw_content": True
})

results = response.json()
```

## 📦 What's inside

- **SearXNG** (port 8999) — powerful metasearch engine
- **Tavily Adapter** (port 8000) — Tavily-compatible HTTP API + `/extract`
- **Redis** (Valkey) — SearXNG cache
- **Unified config** — one `config.yaml` for all services

## 🎯 Why this vs. hosted Tavily

| Tavily (original) | Searcharvester |
|---|---|
| 💰 Paid | ✅ Free |
| 🔑 API key required | ✅ No keys |
| 📊 Request quotas | ✅ No quotas |
| 🏢 External service | ✅ Self-hosted |
| ❓ Opaque sources | ✅ You control the engines |

## 📋 API

### `POST /search` — search

```json
{
  "query": "search query",
  "max_results": 10,
  "include_raw_content": false,
  "engines": "google,duckduckgo,brave",   // optional
  "categories": "general"                   // optional: news/images/videos/map/music/it/science/files/social
}
```

Response — Tavily-compatible schema (see [`docs/en/api.md`](docs/en/api.md)).

### `POST /extract` — page to markdown

```json
{
  "url": "https://example.com/article",
  "size": "m"   // s=5000, m=10000, l=25000 chars, f=full with pagination of 25000
}
```

Response:

```json
{
  "id": "a1b2c3d4e5f60718",
  "url": "...",
  "title": "Article title",
  "format": "md",
  "size": "m",
  "content": "# Title\n\nMarkdown...",
  "chars": 10000,
  "total_chars": 33430,
  "pages": { "current": 1, "total": 1, "page_size": 10000 }
}
```

### `GET /extract/{id}/{page}` — next pages

For `size=f` with long documents. `id` and page number come from the previous `POST /extract`.

Full API reference: [`docs/en/api.md`](docs/en/api.md).

## 🕷️ Markdown extraction (trafilatura)

Page content is extracted via **[trafilatura](https://github.com/adbar/trafilatura)** — a battle-tested main-content extraction library. Output is markdown with headings, lists, tables, links. Navigation/header/footer/ads are stripped automatically.

Two ways to get markdown:

### 1. `include_raw_content` on `/search`

```python
response = client.search(
    query="machine learning",
    max_results=3,
    include_raw_content=True
)
# raw_content = markdown, trimmed to adapter.scraper.max_content_length
```

### 2. `/extract` — dedicated endpoint with size presets

```python
# Quick summary
requests.post("http://localhost:8000/extract", json={
    "url": "...",
    "size": "s"  # 5000 chars
})

# Full article with pagination
r = requests.post("http://localhost:8000/extract", json={
    "url": "...",
    "size": "f"  # no limit, paged at 25000
}).json()

# Next page
requests.get(f"http://localhost:8000/extract/{r['id']}/2")
```

### Tuning

In `config.yaml`:

```yaml
adapter:
  scraper:
    timeout: 10                    # per-page fetch timeout (sec)
    max_content_length: 2500       # raw_content cap in /search
    user_agent: "Mozilla/5.0..."   # User-Agent
```

### Performance

| Endpoint | Response time | Payload |
|---|---|---|
| `/search` without raw_content | ~1–2 s | Snippets only |
| `/search` with raw_content | ~3–5 s | Markdown for every URL |
| `/extract` (cold) | ~1–3 s | Page markdown |
| `/extract/{id}/{page}` (cached) | <50 ms | Next page |

> 💡 **LLM pipeline tip**: call `/search` without `raw_content` → pick top-1–3 URLs → call `/extract` per URL. Faster and gives you control over context size.

## ⚙️ Configuration

Details: [CONFIG_SETUP.md](CONFIG_SETUP.md)

## 🏗️ Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Your code     │───▶│  Tavily Adapter  │───▶│     SearXNG     │
│                 │    │  (FastAPI :8000) │    │   (port 8999)   │
│ requests.post() │    │                  │    │                 │
└─────────────────┘    └──────┬───────────┘    └────────┬────────┘
                              │                         │
                    /extract  │ /search                 │
                              ▼                         ▼
                     ┌──────────────────┐    ┌──────────────────┐
                     │   trafilatura    │    │  Google, Brave,  │
                     │  HTML → markdown │    │  DuckDuckGo, ... │
                     └──────────────────┘    └──────────────────┘
```

Full C4 diagrams (Context / Container / Component + sequences): [`docs/en/architecture.md`](docs/en/architecture.md).

## 🔧 Development

```bash
# Local adapter development
cd simple_tavily_adapter
pip install -r requirements.txt
python main.py

# Smoke test
python test_client.py
```

## 🐳 Pre-built image

Published to GitHub Container Registry:

- `ghcr.io/vakovalskii/searcharvester:latest`
- `ghcr.io/vakovalskii/searcharvester:2.0.0`

`docker-compose.yaml` uses `image:` by default — no build needed. For local dev: `docker compose up --build`.

## 📜 License

MIT — use as you like 🎉
