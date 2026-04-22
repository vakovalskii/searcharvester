# Architecture (C4)

System described in the [C4 model](https://c4model.com/) notation: three levels from broad to narrow — Context, Container, Component. Diagrams are Mermaid — render in GitHub and most IDEs.

---

## Level 1. System Context

How the system looks from the outside: who interacts with it and what external services it talks to.

```mermaid
C4Context
    title System Context — SearXNG Tavily Adapter

    Person(dev, "Developer / LLM agent", "Sends search requests in Tavily API format")

    System(stack, "SearXNG Tavily Adapter", "Self-hosted stack: SearXNG + FastAPI adapter,<br/>Tavily-compatible API")

    System_Ext(engines, "Search engines", "Google, DuckDuckGo, Brave, Bing,<br/>Startpage, etc. (queried by SearXNG)")
    System_Ext(sites, "Target sites", "HTML pages from search results<br/>(scraped when include_raw_content=true)")

    Rel(dev, stack, "POST /search", "HTTP / JSON")
    Rel(stack, engines, "Search queries", "HTTPS")
    Rel(stack, sites, "GET pages (scraping)", "HTTPS")

    UpdateLayoutConfig($c4ShapeInRow="2", $c4BoundaryInRow="1")
```

**System boundaries:**
- Everything inside the `SearXNG Tavily Adapter` frame comes up with a single `docker compose up -d`.
- Search engines and target sites are the public internet — we don't own their availability or rate limits.

---

## Level 2. Container

System split into containers (in C4 sense — "units of deployment"). Here that maps 1:1 to Docker Compose services.

```mermaid
C4Container
    title Container — docker-compose stack

    Person(dev, "Developer / LLM agent")
    System_Ext(engines, "Search engines", "Google, DuckDuckGo, ...")
    System_Ext(sites, "Target sites", "HTML pages")

    System_Boundary(stack, "SearXNG Tavily Adapter stack") {
        Container(adapter, "Tavily Adapter", "Python 3.11, FastAPI, aiohttp", "Accepts Tavily-compatible requests,<br/>proxies to SearXNG,<br/>optionally scrapes pages.<br/>Port 8000 (published)")
        Container(searxng, "SearXNG", "Python, Flask", "Metasearch engine.<br/>Port 8080 internal → 8999 on host")
        ContainerDb(redis, "Valkey (Redis)", "valkey:8-alpine", "Cache and state for SearXNG.<br/>Internal Docker network only")
    }

    Rel(dev, adapter, "POST /search", "HTTP/JSON, 8000")
    Rel(adapter, searxng, "POST /search?format=json", "HTTP, internal network")
    Rel(adapter, sites, "GET pages<br/>(when include_raw_content)", "HTTPS")
    Rel(searxng, engines, "HTTP queries", "HTTPS")
    Rel(searxng, redis, "Cache / sessions", "RESP (Redis protocol)")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

### Breakdown

| Container | Image / build | Host port | Volume / config |
|---|---|---|---|
| `tavily-adapter` | `ghcr.io/vakovalskii/searcharvester:latest` (or build `./simple_tavily_adapter`) | **8000** → 8000 | `./config.yaml:/srv/searxng-docker/config.yaml:ro` |
| `searxng` | `docker.io/searxng/searxng:latest` | **8999** → 8080 | `./config.yaml:/etc/searxng/settings.yml:ro`, `searxng-data:/var/cache/searxng` |
| `redis` | `docker.io/valkey/valkey:8-alpine` | — (not published) | `valkey-data2:/data` |

All three live in the same `searxng` Docker network and address each other by service name (`searxng`, `redis`).

### Key SearXNG env vars (set in `docker-compose.yaml`)

- `SEARXNG_BASE_URL=http://localhost:8999/`
- `BIND_ADDRESS=[::]:8080`

---

## Level 3. Component (inside the Tavily Adapter)

What happens inside the `tavily-adapter` container — Python modules and their roles.

```mermaid
C4Component
    title Component — Tavily Adapter (Python)

    Person(dev, "Client")
    ContainerDb(searxng_box, "SearXNG", "HTTP service")
    Container_Ext(sites, "Target sites", "HTTP")

    Container_Boundary(adapter, "simple_tavily_adapter") {
        Component(fastapi, "main.py — FastAPI app", "POST /search, POST /extract,<br/>GET /extract/{id}/{page}, GET /health", "Endpoints and request validation<br/>(Pydantic: SearchRequest, ExtractRequest)")
        Component(client, "tavily_client.py", "Python class", "In-process client<br/>(for scripts without HTTP)")
        Component(config, "config_loader.py", "Singleton", "Reads config.yaml,<br/>exposes params via properties")
        Component(extractor, "trafilatura.extract()", "Python library", "Main-content extraction →<br/>markdown with headings, links,<br/>tables. Strips nav/footer/ads")
        Component(cache, "_extract_cache", "in-memory dict + TTL", "id → {url, title, content}.<br/>TTL 30 min. Backs /extract/{id}/{page}")
        Component(models, "TavilyResult / TavilyResponse", "Pydantic models", "Response schema for /search (Tavily format)")
    }

    Rel(dev, fastapi, "HTTP/JSON requests")
    Rel(fastapi, config, "reads searxng_url,<br/>scraper_timeout, user_agent")
    Rel(fastapi, searxng_box, "POST /search?format=json", "aiohttp")
    Rel(fastapi, sites, "GET HTML (aiohttp)", "HTTPS")
    Rel(fastapi, extractor, "HTML → markdown")
    Rel(fastapi, cache, "put/get by id")
    Rel(fastapi, models, "builds /search response")
    Rel(client, config, "uses")
    Rel(client, extractor, "uses")
    Rel(client, models, "uses")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

### Sequence: `POST /search`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI (main.py)
    participant S as SearXNG
    participant W as Target sites

    C->>A: POST /search {query, max_results,<br/>engines?, categories?, include_raw_content?}
    A->>A: Validation (Pydantic SearchRequest)
    A->>S: POST /search?format=json&<br/>engines=...&categories=...
    S-->>A: JSON with results[]

    opt include_raw_content == true
        par Parallel fetch + trafilatura (asyncio.gather)
            A->>W: GET url_1
            A->>W: GET url_2
            A->>W: GET url_N
        end
        W-->>A: HTML
        A->>A: trafilatura.extract(<br/>  output_format='markdown')
    end

    A->>A: Map SearXNG results → TavilyResult[]
    A-->>C: TavilyResponse (JSON, raw_content as md)
```

### Sequence: `POST /extract` + pagination

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI
    participant K as _extract_cache
    participant W as Target site

    C->>A: POST /extract {url, size="f"}
    A->>A: id = md5(url)[:16]
    A->>K: lookup id
    alt cache miss
        A->>W: GET url
        W-->>A: HTML
        A->>A: trafilatura.extract(md)
        A->>K: store id → {url, title, content}<br/>(TTL 30 min)
    else cache hit (< 30 min)
        K-->>A: content
    end
    A->>A: slice content by size<br/>(s=5k, m=10k, l=25k, f=25k/page)
    A-->>C: {id, content, pages: {current, total, next?}}

    Note over C,A: Content spans multiple pages → client fetches subsequent ones

    C->>A: GET /extract/{id}/2
    A->>K: lookup id
    K-->>A: content
    A->>A: slice [25000 : 50000]
    A-->>C: {content, pages: {current: 2, total: N, next?}}
```

### Files and their responsibilities

| File | Role |
|---|---|
| `simple_tavily_adapter/main.py` | FastAPI app. Endpoints: `POST /search`, `POST /extract`, `GET /extract/{id}/{page}`, `GET /health`. Houses trafilatura extractor and in-memory cache |
| `simple_tavily_adapter/tavily_client.py` | Python class `TavilyClient`, mirroring `tavily-python` API. For scripts that don't want HTTP |
| `simple_tavily_adapter/config_loader.py` | Reads unified `config.yaml`, exposes params via `@property` |
| `simple_tavily_adapter/Dockerfile` | `python:3.11-slim` + `curl` for health-check. Runs `uvicorn main:app` |
| `simple_tavily_adapter/requirements.txt` | FastAPI, aiohttp, **trafilatura**, **lxml**, pydantic, pyyaml |
| `simple_tavily_adapter/test_client.py` | Smoke test for `TavilyClient` |

### Config actually read by the code

- `adapter.searxng_url` → where the adapter calls
- `adapter.server.host`, `adapter.server.port` → uvicorn bind
- `adapter.scraper.timeout` → per-page timeout
- `adapter.scraper.max_content_length` → `raw_content` length limit
- `adapter.scraper.user_agent` → User-Agent for scraping

**Not read by the code (hardcoded)**: `adapter.search.default_engines`, `default_categories`, `default_language`, `safesearch`, `default_max_results`. They exist as properties in `config_loader.py` but aren't applied in `main.py`. Known wart — see [`../../CLAUDE.md`](../../CLAUDE.md).

---

## Deployment view

```mermaid
flowchart LR
    subgraph Host["Host (Mac/Linux)"]
        subgraph docker["Docker Engine (Colima / Docker Desktop / native)"]
            subgraph net["network: searxng (bridge)"]
                A[tavily-adapter<br/>:8000]
                S[searxng<br/>:8080]
                R[(redis/valkey<br/>:6379)]
            end
            V1[(volume:<br/>searxng-data)]
            V2[(volume:<br/>valkey-data2)]
        end
        FS[(config.yaml<br/>on filesystem)]
    end

    Client[Client<br/>curl / code / LLM] -- :8000 --> A
    Browser[Browser] -- :8999 --> S

    A --> S
    S --> R
    S --- V1
    R --- V2

    FS -. bind mount:ro .-> A
    FS -. bind mount:ro .-> S
```

A single `config.yaml` is mounted read-only into two containers: SearXNG sees it as its `settings.yml`, the adapter sees it as its config. This is deliberate — avoids keeping two files in sync.

---

## What's intentionally simplified

- **No HTTPS / reverse proxy.** The repo has a `Caddyfile` inherited from upstream `searxng-docker`, but it's not wired into `docker-compose.yaml`. If you need TLS, add a Caddy service and expose 80/443.
- **No limiter / auth.** `limiter: false` in config — fine for a local machine, not fine for a public endpoint.
- **`/search` score is fake** (`0.9 - i*0.05`). SearXNG does provide real relevance, but the adapter doesn't forward it.
- **`/extract` cache is in-memory, no persistence.** After container restart, stale ids require a fresh `POST /extract`. TTL 30 minutes.
- **`/extract` — one URL per call.** Batch extraction (list of URLs) isn't implemented.
