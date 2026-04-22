# Project Overview

## What it is

**Searcharvester** is a ready-to-run Docker Compose stack that provides a self-hosted alternative to the [Tavily](https://tavily.com) API. You spin up the stack, get an HTTP endpoint that's Tavily-compatible, and wire it into your code or LLM pipelines without changing application logic.

The stack is three services:

1. **SearXNG** — a metasearch engine that aggregates results from Google, DuckDuckGo, Brave, and many others.
2. **Tavily Adapter** — a FastAPI wrapper over SearXNG, extended with trafilatura for main-content extraction. Three endpoints:
   - `POST /search` — Tavily-compatible search (+ optional markdown `raw_content`)
   - `POST /extract` — extract a page to markdown with size presets (s/m/l/f)
   - `GET /extract/{id}/{page}` — pagination for long documents
3. **Redis (Valkey)** — cache for SearXNG.

## Why

| Problem | Solution |
|---|---|
| Tavily costs money, has quotas | Adapter is free, no quotas — you're only limited by the search engines themselves |
| Need control over search sources | Enable/disable any SearXNG engines in `config.yaml` |
| Requests shouldn't leave the local network | Everything runs inside a local Docker network |
| Need clean page text for LLM input | Adapter can optionally scrape URLs and return markdown via `raw_content` or `/extract` |

## Core components

### SearXNG
- Image: `docker.io/searxng/searxng:latest`
- Internal port `8080`, published on host as **`8999`**
- Configured via the same `config.yaml` (mounted as `/etc/searxng/settings.yml`)
- Uses Valkey/Redis for cache and session storage

### Tavily Adapter
- Built from `simple_tavily_adapter/` (Python 3.11, FastAPI, aiohttp, **trafilatura 2.x**)
- Published on port **`8000`**
- Four endpoints:
  - `POST /search` — Tavily-compatible search + engine/category selection
  - `POST /extract` — extract a page to markdown with size presets
  - `GET /extract/{id}/{page}` — pagination for long documents (cache TTL 30 min)
  - `GET /health` — Docker health-check
- Reads `config.yaml` from `/srv/searxng-docker/config.yaml` (volume-mounted)
- Page scraping runs in parallel via `asyncio.gather`
- Main-content extraction via **trafilatura** (readability++): strips nav/footer/ads, emits markdown with headings, lists, tables, links

### Redis (Valkey)
- Image: `docker.io/valkey/valkey:8-alpine`
- Not published on the host; reachable only within the `searxng` Docker network
- SearXNG uses it for caching and limiter state

## How it fits together

```
┌──────────────┐   HTTP/JSON    ┌────────────────┐   HTTP/JSON    ┌──────────┐
│ Your code /  │───────────────▶│ Tavily Adapter │───────────────▶│ SearXNG  │
│  LLM / curl  │◀───────────────│   (port 8000)  │◀───────────────│ (port    │
└──────────────┘    Tavily      └────────┬───────┘   SearXNG      │  8999)   │
                    format               │                         └────┬─────┘
                                         │ aiohttp + trafilatura        │
                                         ▼                              ▼
                                  ┌──────────────┐              ┌──────────────┐
                                  │ Target sites │              │ Redis/Valkey │
                                  │ (scraped for │              │   (cache)    │
                                  │  markdown)   │              └──────────────┘
                                  └──────────────┘
```

See full C4 diagrams in [architecture.md](architecture.md).

## Next

- Run from scratch → [getting-started.md](getting-started.md)
- API reference → [api.md](api.md)
- Ops (logs, debugging, troubleshooting) → [operations.md](operations.md)
