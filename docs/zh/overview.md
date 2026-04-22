# 项目概览

## 这是什么

**SearXNG Docker Tavily Adapter** 是一个开箱即用的 Docker Compose 技术栈，为 [Tavily](https://tavily.com) API 提供自托管替代方案。启动这个技术栈后，你就能获得一个完全兼容 Tavily 的 HTTP 端点，可以直接接入你的代码或 LLM 流水线，无需修改任何业务逻辑。

技术栈内部由三个服务组成：

1. **SearXNG** — 元搜索引擎，聚合来自 Google、DuckDuckGo、Brave 等引擎的结果。
2. **Tavily Adapter** — 基于 FastAPI 的 SearXNG 封装层，通过 trafilatura 扩展正文提取能力。提供三个端点：
   - `POST /search` — 搜索 + 摘要（可选 markdown raw_content）
   - `POST /extract` — 以 markdown 形式提取页面，支持尺寸预设 (s/m/l/f)
   - `GET /extract/{id}/{page}` — 长文档分页
3. **Redis (Valkey)** — SearXNG 的缓存。

## 为什么需要它

| 问题 | 解决方案 |
|---|---|
| Tavily 收费，有调用限额 | 本适配器免费，无限额 — 仅受搜索引擎自身限额约束 |
| 需要控制搜索来源 | 在 `config.yaml` 中自由启用/禁用任何 SearXNG 引擎 |
| 查询不应发往外部服务 | 所有组件运行在本地 docker 网络内 |
| 需要给 LLM 提供页面原文 | 适配器可选地抓取 URL 并通过 `raw_content` 返回正文 |

## 主要组件

### SearXNG
- 镜像 `docker.io/searxng/searxng:latest`
- 容器内端口 `8080`，在宿主机上发布为 **`8999`**
- 使用同一份 `config.yaml`（挂载为 `/etc/searxng/settings.yml`）
- 使用 Valkey/Redis 作为缓存和会话存储

### Tavily Adapter
- 由 `simple_tavily_adapter/` 构建 (Python 3.11, FastAPI, aiohttp, **trafilatura 2.x**)
- 发布端口 **`8000`**
- 四个端点：
  - `POST /search` — Tavily 兼容搜索，支持选择引擎/分类
  - `POST /extract` — 提取页面为 markdown，支持尺寸预设
  - `GET /extract/{id}/{page}` — 长文档分页（缓存 TTL 30 分钟）
  - `GET /health` — 供 Docker 使用的健康检查
- 从 `/srv/searxng-docker/config.yaml` 读取配置（以 volume 方式挂载）
- 页面抓取通过 `asyncio.gather` 并行执行
- 正文提取通过 **trafilatura** (readability++)：剥离 nav/footer/ads，输出带标题、列表、表格、链接的 markdown

### Redis (Valkey)
- 镜像 `docker.io/valkey/valkey:8-alpine`
- 不向宿主机发布端口，仅在 docker 网络 `searxng` 内部可达
- 供 SearXNG 做缓存和限流器状态存储

## 各部分如何协作

```
┌──────────────┐   HTTP/JSON    ┌────────────────┐   HTTP/JSON    ┌──────────┐
│  你的代码 /  │───────────────▶│ Tavily Adapter │───────────────▶│ SearXNG  │
│  LLM / curl  │◀───────────────│   (端口 8000)  │◀───────────────│ (端口    │
└──────────────┘    Tavily      └────────┬───────┘   SearXNG      │  8999)   │
                   格式                  │                         └────┬─────┘
                                         │ aiohttp + BeautifulSoup      │
                                         ▼                              ▼
                                  ┌──────────────┐              ┌──────────────┐
                                  │ 结果         │              │ Redis/Valkey │
                                  │ 网站         │              │   (缓存)     │
                                  │ (抓取)       │              └──────────────┘
                                  └──────────────┘
```

完整的 C4 图请参阅 [architecture.md](architecture.md)。

## 接下来

- 从零启动 → [getting-started.md](getting-started.md)
- API 格式 → [api.md](api.md)
- 运维（日志、调试、故障排查）→ [operations.md](operations.md)
