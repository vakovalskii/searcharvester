# 架构 (C4)

采用 [C4 model](https://c4model.com/) 的三层视图，由粗到细：Context、Container、Component。图表使用 Mermaid 绘制，可在 GitHub 和大多数 IDE 中直接渲染。

---

## 第 1 层. System Context

系统的对外视角：谁在和它交互，以及它依赖哪些外部服务。

```mermaid
C4Context
    title System Context — SearXNG Tavily Adapter

    Person(dev, "开发者 / LLM 智能体", "以 Tavily API 格式发送搜索请求")

    System(stack, "SearXNG Tavily Adapter", "自托管技术栈：SearXNG + FastAPI 适配器，<br/>兼容 Tavily API")

    System_Ext(engines, "搜索引擎", "Google、DuckDuckGo、Brave、Bing、<br/>Startpage 等 (SearXNG 查询它们)")
    System_Ext(sites, "目标网站", "搜索结果中的 HTML 页面<br/>(当 include_raw_content=true 时被抓取)")

    Rel(dev, stack, "POST /search", "HTTP / JSON")
    Rel(stack, engines, "搜索请求", "HTTPS")
    Rel(stack, sites, "GET 页面 (抓取)", "HTTPS")

    UpdateLayoutConfig($c4ShapeInRow="2", $c4BoundaryInRow="1")
```

**系统边界：**
- `SearXNG Tavily Adapter` 框内的一切，通过一条 `docker compose up -d` 命令即可启动。
- 搜索引擎与目标网站属于公共互联网，其可用性与限流不在本项目控制范围内。

---

## 第 2 层. Container

把系统拆分为容器（C4 语境下的"部署单元"）。在本项目中与 Docker Compose 服务一一对应。

```mermaid
C4Container
    title Container — docker-compose 技术栈

    Person(dev, "开发者 / LLM 智能体")
    System_Ext(engines, "搜索引擎", "Google、DuckDuckGo、...")
    System_Ext(sites, "目标网站", "HTML 页面")

    System_Boundary(stack, "SearXNG Tavily Adapter stack") {
        Container(adapter, "Tavily Adapter", "Python 3.11, FastAPI, aiohttp", "接收 Tavily 兼容请求，<br/>代理到 SearXNG，<br/>可选地抓取页面。<br/>端口 8000 (对外发布)")
        Container(searxng, "SearXNG", "Python, Flask", "元搜索引擎。<br/>容器内 8080 → 宿主机 8999")
        ContainerDb(redis, "Valkey (Redis)", "valkey:8-alpine", "SearXNG 的缓存与状态。<br/>仅 docker 网络内可达")
    }

    Rel(dev, adapter, "POST /search", "HTTP/JSON, 8000")
    Rel(adapter, searxng, "POST /search?format=json", "HTTP, 内部网络")
    Rel(adapter, sites, "GET 页面<br/>(当 include_raw_content)", "HTTPS")
    Rel(searxng, engines, "HTTP 请求", "HTTPS")
    Rel(searxng, redis, "缓存 / 会话", "RESP (Redis protocol)")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

### 分项

| 容器 | 镜像 / 构建 | 宿主机端口 | Volume / 配置 |
|---|---|---|---|
| `tavily-adapter` | build `./simple_tavily_adapter` | **8000** → 8000 | `./config.yaml:/srv/searxng-docker/config.yaml:ro` |
| `searxng` | `docker.io/searxng/searxng:latest` | **8999** → 8080 | `./config.yaml:/etc/searxng/settings.yml:ro`, `searxng-data:/var/cache/searxng` |
| `redis` | `docker.io/valkey/valkey:8-alpine` | — (不发布) | `valkey-data2:/data` |

三个容器都位于同一 docker 网络 `searxng`，通过服务名 (`searxng`、`redis`) 相互访问。

### SearXNG 关键环境变量（在 `docker-compose.yaml` 中设置）

- `SEARXNG_BASE_URL=http://localhost:8999/`
- `BIND_ADDRESS=[::]:8080`

---

## 第 3 层. Component (Tavily Adapter 内部)

`tavily-adapter` 容器内部发生了什么 — Python 代码模块及其职责。

```mermaid
C4Component
    title Component — Tavily Adapter (Python)

    Person(dev, "客户端")
    ContainerDb(searxng_box, "SearXNG", "HTTP 服务")
    Container_Ext(sites, "目标网站", "HTTP")

    Container_Boundary(adapter, "simple_tavily_adapter") {
        Component(fastapi, "main.py — FastAPI app", "POST /search, POST /extract,<br/>GET /extract/{id}/{page}, GET /health", "端点与请求校验<br/>(Pydantic: SearchRequest, ExtractRequest)")
        Component(client, "tavily_client.py", "Python class", "进程内客户端<br/>(面向无需 HTTP 的脚本)")
        Component(config, "config_loader.py", "Singleton", "读取 config.yaml，<br/>通过 properties 暴露参数")
        Component(extractor, "trafilatura.extract()", "Python library", "正文提取 →<br/>带标题、链接、表格的 markdown。<br/>自动剥离 nav/footer/ads")
        Component(cache, "_extract_cache", "in-memory dict + TTL", "id → {url, title, content}。<br/>TTL 30 分钟。供 /extract/{id}/{page} 使用")
        Component(models, "TavilyResult / TavilyResponse", "Pydantic models", "/search 响应 schema (Tavily 格式)")
    }

    Rel(dev, fastapi, "HTTP/JSON 请求")
    Rel(fastapi, config, "读取 searxng_url、<br/>scraper_timeout、user_agent")
    Rel(fastapi, searxng_box, "POST /search?format=json", "aiohttp")
    Rel(fastapi, sites, "GET HTML (aiohttp)", "HTTPS")
    Rel(fastapi, extractor, "HTML → markdown")
    Rel(fastapi, cache, "按 id 存取")
    Rel(fastapi, models, "构建 /search 响应")
    Rel(client, config, "使用")
    Rel(client, extractor, "使用")
    Rel(client, models, "使用")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

### Sequence: `POST /search`

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI (main.py)
    participant S as SearXNG
    participant W as 目标网站

    C->>A: POST /search {query, max_results,<br/>engines?, categories?, include_raw_content?}
    A->>A: 校验 (Pydantic SearchRequest)
    A->>S: POST /search?format=json&<br/>engines=...&categories=...
    S-->>A: JSON，含 results[] 列表

    opt include_raw_content == true
        par 并行 fetch + trafilatura (asyncio.gather)
            A->>W: GET url_1
            A->>W: GET url_2
            A->>W: GET url_N
        end
        W-->>A: HTML
        A->>A: trafilatura.extract(<br/>  output_format='markdown')
    end

    A->>A: 将 SearXNG results 映射为 TavilyResult[]
    A-->>C: TavilyResponse (JSON，raw_content 为 md)
```

### Sequence: `POST /extract` + 分页

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI
    participant K as _extract_cache
    participant W as 目标网站

    C->>A: POST /extract {url, size="f"}
    A->>A: id = md5(url)[:16]
    A->>K: lookup id
    alt cache miss
        A->>W: GET url
        W-->>A: HTML
        A->>A: trafilatura.extract(md)
        A->>K: 存储 id → {url, title, content}<br/>(TTL 30 分钟)
    else cache hit (< 30 分钟)
        K-->>A: content
    end
    A->>A: 按 size 切分内容<br/>(s=5k, m=10k, l=25k, f=25k/页)
    A-->>C: {id, content, pages: {current, total, next?}}

    Note over C,A: 内容超过一页 → 客户端继续获取下一页

    C->>A: GET /extract/{id}/2
    A->>K: lookup id
    K-->>A: content
    A->>A: slice [25000 : 50000]
    A-->>C: {content, pages: {current: 2, total: N, next?}}
```

### 文件与职责

| 文件 | 职责 |
|---|---|
| `simple_tavily_adapter/main.py` | FastAPI 应用。端点：`POST /search`、`POST /extract`、`GET /extract/{id}/{page}`、`GET /health`。包含 trafilatura 提取器和内存缓存 |
| `simple_tavily_adapter/tavily_client.py` | Python 类 `TavilyClient`，镜像 `tavily-python` 的 API。面向无需 HTTP 的脚本 |
| `simple_tavily_adapter/config_loader.py` | 读取统一的 `config.yaml`，通过 `@property` 暴露参数 |
| `simple_tavily_adapter/Dockerfile` | `python:3.11-slim` + 用于健康检查的 `curl`。启动 `uvicorn main:app` |
| `simple_tavily_adapter/requirements.txt` | FastAPI, aiohttp, **trafilatura**, **lxml**, pydantic, pyyaml |
| `simple_tavily_adapter/test_client.py` | `TavilyClient` 的冒烟测试 |

### 代码实际读取的配置项

- `adapter.searxng_url` → 适配器访问 SearXNG 的地址
- `adapter.server.host`、`adapter.server.port` → uvicorn 绑定地址
- `adapter.scraper.timeout` → 单页抓取超时
- `adapter.scraper.max_content_length` → `raw_content` 长度上限
- `adapter.scraper.user_agent` → 抓取时使用的 User-Agent

**未被代码读取（已硬编码）**：`adapter.search.default_engines`、`default_categories`、`default_language`、`safesearch`、`default_max_results`。这些字段在 `config_loader.py` 中作为 property 存在，但未在 `main.py` 中应用。属已知的遗留问题 — 详见 [`../CLAUDE.md`](../CLAUDE.md)。

---

## 部署 (Deployment view)

```mermaid
flowchart LR
    subgraph Host["宿主机 (Mac/Linux)"]
        subgraph docker["Docker Engine (Colima / Docker Desktop / 原生)"]
            subgraph net["network: searxng (bridge)"]
                A[tavily-adapter<br/>:8000]
                S[searxng<br/>:8080]
                R[(redis/valkey<br/>:6379)]
            end
            V1[(volume:<br/>searxng-data)]
            V2[(volume:<br/>valkey-data2)]
        end
        FS[(config.yaml<br/>位于文件系统)]
    end

    Client[客户端<br/>curl / 代码 / LLM] -- :8000 --> A
    Browser[浏览器] -- :8999 --> S

    A --> S
    S --> R
    S --- V1
    R --- V2

    FS -. bind mount:ro .-> A
    FS -. bind mount:ro .-> S
```

同一份 `config.yaml` 以只读方式挂载到两个容器：对 SearXNG 来说是 `settings.yml`，对适配器来说是它的配置文件。这是刻意设计的：避免维护两份相互同步的文件。

---

## 刻意简化之处

- **没有 HTTPS / 反向代理。** 仓库中保留了 upstream `searxng-docker` 的 `Caddyfile`，但未在 `docker-compose.yaml` 中启用。如果需要 TLS — 添加 Caddy 服务并暴露 80/443。
- **没有限流器 / 鉴权。** 配置中的 `limiter: false` 对本地开发够用，对公网端点则不适合。
- **`/search` 中的 score 是伪造的** (`0.9 - i*0.05`)。SearXNG 本身提供真实相关度，但适配器未透传。
- **`/extract` 缓存是内存态，无持久化。** 容器重启后，已过期的 id 需要重新发起 `POST /extract`。TTL 30 分钟。
- **`/extract` 每次调用仅处理一个 URL。** 未实现批量提取（URL 列表）。
