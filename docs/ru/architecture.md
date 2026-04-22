# Архитектура (C4)

Описание системы в нотации [C4 model](https://c4model.com/): три уровня от общего к частному — Context, Container, Component. Диаграммы в Mermaid — рендерятся в GitHub и большинстве IDE.

---

## Уровень 1. System Context

Как система видна снаружи: кто с ней взаимодействует и с какими внешними сервисами она работает.

```mermaid
C4Context
    title System Context — SearXNG Tavily Adapter

    Person(dev, "Разработчик / LLM-агент", "Шлёт поисковые запросы в формате Tavily API")

    System(stack, "SearXNG Tavily Adapter", "Self-hosted стек: SearXNG + FastAPI-адаптер,<br/>совместимый с Tavily API")

    System_Ext(engines, "Поисковые движки", "Google, DuckDuckGo, Brave, Bing,<br/>Startpage и др. (SearXNG их опрашивает)")
    System_Ext(sites, "Целевые сайты", "HTML-страницы из результатов поиска<br/>(скрапятся при include_raw_content=true)")

    Rel(dev, stack, "POST /search", "HTTP / JSON")
    Rel(stack, engines, "Поисковые запросы", "HTTPS")
    Rel(stack, sites, "GET страниц (скрапинг)", "HTTPS")

    UpdateLayoutConfig($c4ShapeInRow="2", $c4BoundaryInRow="1")
```

**Границы системы:**
- Всё, что внутри рамки `SearXNG Tavily Adapter`, — поднимается одной командой `docker compose up -d`.
- Поисковые движки и целевые сайты — это публичный интернет, за их доступность и rate limits мы не отвечаем.

---

## Уровень 2. Container

Разбивка системы на контейнеры (в смысле C4 — «единицы деплоя»). В нашем случае совпадает с сервисами Docker Compose.

```mermaid
C4Container
    title Container — docker-compose стек

    Person(dev, "Разработчик / LLM-агент")
    System_Ext(engines, "Поисковые движки", "Google, DuckDuckGo, ...")
    System_Ext(sites, "Целевые сайты", "HTML-страницы")

    System_Boundary(stack, "SearXNG Tavily Adapter stack") {
        Container(adapter, "Tavily Adapter", "Python 3.11, FastAPI, aiohttp", "Принимает Tavily-совместимые запросы,<br/>проксирует в SearXNG,<br/>опционально скрапит страницы.<br/>Порт 8000 (публикуется)")
        Container(searxng, "SearXNG", "Python, Flask", "Мета-поисковик.<br/>Порт 8080 внутри → 8999 на хосте")
        ContainerDb(redis, "Valkey (Redis)", "valkey:8-alpine", "Кеш и состояние SearXNG.<br/>Только внутри docker-сети")
    }

    Rel(dev, adapter, "POST /search", "HTTP/JSON, 8000")
    Rel(adapter, searxng, "POST /search?format=json", "HTTP, внутренняя сеть")
    Rel(adapter, sites, "GET страницы<br/>(при include_raw_content)", "HTTPS")
    Rel(searxng, engines, "HTTP-запросы", "HTTPS")
    Rel(searxng, redis, "Кеш / сессии", "RESP (Redis protocol)")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

### Разрезы

| Контейнер | Образ / сборка | Порт хоста | Volume / конфиг |
|---|---|---|---|
| `tavily-adapter` | build `./simple_tavily_adapter` | **8000** → 8000 | `./config.yaml:/srv/searxng-docker/config.yaml:ro` |
| `searxng` | `docker.io/searxng/searxng:latest` | **8999** → 8080 | `./config.yaml:/etc/searxng/settings.yml:ro`, `searxng-data:/var/cache/searxng` |
| `redis` | `docker.io/valkey/valkey:8-alpine` | — (не публикуется) | `valkey-data2:/data` |

Все три сидят в одной docker-сети `searxng`, общаются по именам сервисов (`searxng`, `redis`).

### Ключевые env-переменные SearXNG (задаются в `docker-compose.yaml`)

- `SEARXNG_BASE_URL=http://localhost:8999/`
- `BIND_ADDRESS=[::]:8080`

---

## Уровень 3. Component (внутри Tavily Adapter)

Что происходит внутри контейнера `tavily-adapter` — модули Python-кода и их роли.

```mermaid
C4Component
    title Component — Tavily Adapter (Python)

    Person(dev, "Клиент")
    ContainerDb(searxng_box, "SearXNG", "HTTP сервис")
    Container_Ext(sites, "Целевые сайты", "HTTP")

    Container_Boundary(adapter, "simple_tavily_adapter") {
        Component(fastapi, "main.py — FastAPI app", "POST /search, POST /extract,<br/>GET /extract/{id}/{page}, GET /health", "Эндпойнты и валидация запросов<br/>(Pydantic: SearchRequest, ExtractRequest)")
        Component(client, "tavily_client.py", "Python class", "In-process клиент<br/>(для скриптов без HTTP)")
        Component(config, "config_loader.py", "Singleton", "Читает config.yaml,<br/>отдаёт параметры через properties")
        Component(extractor, "trafilatura.extract()", "Python library", "Main-content extraction →<br/>markdown с заголовками, ссылками,<br/>таблицами. Убирает nav/footer/ads")
        Component(cache, "_extract_cache", "in-memory dict + TTL", "id → {url, title, content}.<br/>TTL 30 мин. Нужен для /extract/{id}/{page}")
        Component(models, "TavilyResult / TavilyResponse", "Pydantic models", "Схема ответа /search (Tavily-формат)")
    }

    Rel(dev, fastapi, "HTTP/JSON запросы")
    Rel(fastapi, config, "читает searxng_url,<br/>scraper_timeout, user_agent")
    Rel(fastapi, searxng_box, "POST /search?format=json", "aiohttp")
    Rel(fastapi, sites, "GET HTML (aiohttp)", "HTTPS")
    Rel(fastapi, extractor, "HTML → markdown")
    Rel(fastapi, cache, "put/get по id")
    Rel(fastapi, models, "строит /search ответ")
    Rel(client, config, "использует")
    Rel(client, extractor, "использует")
    Rel(client, models, "использует")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

### Sequence: `POST /search`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI (main.py)
    participant S as SearXNG
    participant W as Целевые сайты

    C->>A: POST /search {query, max_results,<br/>engines?, categories?, include_raw_content?}
    A->>A: Валидация (Pydantic SearchRequest)
    A->>S: POST /search?format=json&<br/>engines=...&categories=...
    S-->>A: JSON со списком results[]

    opt include_raw_content == true
        par Параллельный fetch + trafilatura (asyncio.gather)
            A->>W: GET url_1
            A->>W: GET url_2
            A->>W: GET url_N
        end
        W-->>A: HTML
        A->>A: trafilatura.extract(<br/>  output_format='markdown')
    end

    A->>A: Мапим SearXNG results → TavilyResult[]
    A-->>C: TavilyResponse (JSON, raw_content в md)
```

### Sequence: `POST /extract` + пагинация

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI
    participant K as _extract_cache
    participant W as Целевой сайт

    C->>A: POST /extract {url, size="f"}
    A->>A: id = md5(url)[:16]
    A->>K: lookup id
    alt cache miss
        A->>W: GET url
        W-->>A: HTML
        A->>A: trafilatura.extract(md)
        A->>K: store id → {url, title, content}<br/>(TTL 30 мин)
    else cache hit (< 30 мин)
        K-->>A: content
    end
    A->>A: нарезка контента по size<br/>(s=5k, m=10k, l=25k, f=25k/page)
    A-->>C: {id, content, pages: {current, total, next?}}

    Note over C,A: Контент длиннее одной страницы → клиент берёт следующие

    C->>A: GET /extract/{id}/2
    A->>K: lookup id
    K-->>A: content
    A->>A: slice [25000 : 50000]
    A-->>C: {content, pages: {current: 2, total: N, next?}}
```

### Файлы и их ответственность

| Файл | Роль |
|---|---|
| `simple_tavily_adapter/main.py` | FastAPI-приложение. Эндпойнты: `POST /search`, `POST /extract`, `GET /extract/{id}/{page}`, `GET /health`. Содержит trafilatura-экстрактор и in-memory кеш |
| `simple_tavily_adapter/tavily_client.py` | Python-класс `TavilyClient`, зеркалирующий API `tavily-python`. Для скриптов без HTTP |
| `simple_tavily_adapter/config_loader.py` | Читает единый `config.yaml`, отдаёт параметры через `@property` |
| `simple_tavily_adapter/Dockerfile` | `python:3.11-slim` + `curl` для health-check. Запускает `uvicorn main:app` |
| `simple_tavily_adapter/requirements.txt` | FastAPI, aiohttp, **trafilatura**, **lxml**, pydantic, pyyaml |
| `simple_tavily_adapter/test_client.py` | Smoke-тест для `TavilyClient` |

### Конфигурация, которая реально читается кодом

- `adapter.searxng_url` → куда адаптер стучится
- `adapter.server.host`, `adapter.server.port` → uvicorn bind
- `adapter.scraper.timeout` → таймаут на одну страницу
- `adapter.scraper.max_content_length` → размер `raw_content`
- `adapter.scraper.user_agent` → User-Agent при скрапинге

**Не читаются кодом (захардкожено)**: `adapter.search.default_engines`, `default_categories`, `default_language`, `safesearch`, `default_max_results`. Они есть в `config_loader.py` как property, но не применяются в `main.py`. Это известная шероховатость — см. [`../CLAUDE.md`](../CLAUDE.md).

---

## Деплой (Deployment view)

```mermaid
flowchart LR
    subgraph Host["Хост (Mac/Linux)"]
        subgraph docker["Docker Engine (Colima / Docker Desktop / нативный)"]
            subgraph net["network: searxng (bridge)"]
                A[tavily-adapter<br/>:8000]
                S[searxng<br/>:8080]
                R[(redis/valkey<br/>:6379)]
            end
            V1[(volume:<br/>searxng-data)]
            V2[(volume:<br/>valkey-data2)]
        end
        FS[(config.yaml<br/>на файловой системе)]
    end

    Client[Клиент<br/>curl / код / LLM] -- :8000 --> A
    Browser[Браузер] -- :8999 --> S

    A --> S
    S --> R
    S --- V1
    R --- V2

    FS -. bind mount:ro .-> A
    FS -. bind mount:ro .-> S
```

Один файл `config.yaml` монтируется в два контейнера read-only: в SearXNG как его `settings.yml`, в адаптер — как его конфиг. Это сделано осознанно: чтобы не держать два синхронизированных файла.

---

## Что сознательно упрощено

- **Нет HTTPS / reverse proxy.** В репе лежит `Caddyfile` от upstream `searxng-docker`, но в `docker-compose.yaml` он не подключён. Если нужен TLS — добавить сервис Caddy и пробросить 80/443.
- **Нет лимитера / auth.** `limiter: false` в конфиге — ок для локальной машины, не ок для публичного эндпойнта.
- **Скор результата в `/search` — фейковый** (`0.9 - i*0.05`). Real relevance SearXNG даёт, но адаптер его не пробрасывает.
- **Кеш `/extract` — in-memory, без персистентности.** После рестарта контейнера просроченные id требуют повторного `POST /extract`. TTL 30 минут.
- **`/extract` — один URL за вызов.** Batch-извлечение (список URL) не реализовано.
