# 适配器 API

基础 URL：`http://localhost:8000`（或你为 `tavily-adapter` 容器的 `8000` 端口映射到的地址）。

没有鉴权。请求中的任意 `api_key` 都会被忽略 — 保留它只是为了兼容 Tavily 客户端。

端点：

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/search` | 搜索：链接 + 摘要（可选 markdown raw_content） |
| `POST` | `/extract` | 将页面提取为 markdown (s/m/l/f + 分页) |
| `GET` | `/extract/{id}/{page}` | 获取已提取内容的下一页（仅 `size=f` 有效） |
| `GET` | `/health` | 健康检查 |

---

## `POST /search`

Tavily 兼容搜索。接收 JSON，返回 JSON。

### 请求

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `query` | string | — | 必填，搜索关键词文本 |
| `max_results` | int | `10` | 返回多少条结果 |
| `include_raw_content` | bool | `false` | `true` → 下载页面并将 markdown 放入 `raw_content`（按 `adapter.scraper.max_content_length` 截断） |
| `engines` | string \| null | `"google,duckduckgo,brave"` | 以逗号分隔的 SearXNG 引擎列表。示例：`"google"`、`"duckduckgo,brave"`、`"yandex,mojeek"` |
| `categories` | string \| null | `"general"` | SearXNG 分类：`general`、`news`、`images`、`videos`、`map`、`music`、`it`、`science`、`files`、`social` |

示例：

```bash
curl -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "最新 AI 新闻",
    "max_results": 5,
    "engines": "duckduckgo,brave",
    "categories": "news"
  }'
```

### 响应

Tavily schema（为保持向后兼容未作改动）：

```json
{
  "query": "最新 AI 新闻",
  "follow_up_questions": null,
  "answer": null,
  "images": [],
  "results": [
    {
      "url": "https://example.com/article",
      "title": "AI Breakthrough 2026",
      "content": "搜索引擎返回的简短摘要...",
      "score": 0.9,
      "raw_content": "# AI Breakthrough 2026\n\nMarkdown 内容..."
    }
  ],
  "response_time": 1.42,
  "request_id": "uuid"
}
```

**`raw_content`** 现在以 **markdown** 格式返回（经 trafilatura 处理）— 包含标题、列表、链接。此前是纯文本。

### 错误码

| 码 | 场景 |
|---|---|
| `200` | 成功（即使 `results` 为空） |
| `422` | 非法 JSON / 缺少 `query` |
| `500` | SearXNG 故障 |
| `504` | SearXNG 30 秒内未响应 |

---

## `POST /extract`

下载页面，提取正文（trafilatura），返回 markdown。

### 请求

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `url` | string | — | 必填，页面 URL |
| `size` | `"s"` \| `"m"` \| `"l"` \| `"f"` | `"m"` | 尺寸预设（见下文） |

**尺寸预设：**

| 值 | 行为 | 适用场景 |
|---|---|---|
| `s` | 截断至 **5 000** 字符 | 面向小上下文 LLM 的简要摘录 |
| `m` | 截断至 **10 000** 字符 | 智能体阅读的常规大小 |
| `l` | 截断至 **25 000** 字符 | 深度阅读文章 |
| `f` | **完整**内容，按每页 25 000 字符分页 | 需要全文时 |

### 响应 (size ∈ s/m/l)

```json
{
  "id": "b275618ca10e6c62",
  "url": "https://en.wikipedia.org/wiki/Docker_(software)",
  "title": "Docker (software) - Wikipedia",
  "format": "md",
  "size": "m",
  "content": "# Docker (software)\n\n...",
  "chars": 10000,
  "total_chars": 33430,
  "pages": { "current": 1, "total": 1, "page_size": 10000 }
}
```

- `chars` — 本次响应中 `content` 的长度。
- `total_chars` — 提取内容**在截断之前**的完整长度（有助于判断是否丢失了上下文）。
- `s/m/l` 不分页 — `pages.total: 1`。

### 响应 (size = f)

若内容可以塞进一页（≤ 25 000 字符）：

```json
{
  "id": "b275618ca10e6c62",
  "url": "...",
  "title": "...",
  "format": "md",
  "size": "f",
  "content": "...",
  "chars": 18500,
  "total_chars": 18500,
  "pages": { "current": 1, "total": 1, "page_size": 25000 }
}
```

若内容超过 25 000 字符：

```json
{
  "id": "b275618ca10e6c62",
  "url": "...",
  "title": "...",
  "format": "md",
  "size": "f",
  "content": "前 25000 个字符 markdown...",
  "chars": 25000,
  "total_chars": 67000,
  "pages": {
    "current": 1,
    "total": 3,
    "page_size": 25000,
    "next": "/extract/b275618ca10e6c62/2"
  }
}
```

`pages.next` 字段直接给出下一页的 `GET` 路径。

### 错误码

| 码 | 场景 |
|---|---|
| `200` | 成功 |
| `422` | 页面没有可提取的内容（trafilatura 返回空） |
| `502` | URL 下载失败（非 200 响应 / 网络错误） |

---

## `GET /extract/{id}/{page}`

返回此前已提取内容的指定页。仅对以 `size=f` 发起请求的文档有效。

### 路径参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `id` | string (16 位 hex) | 此前 `POST /extract` 返回的 `id` |
| `page` | int ≥ 1 | 页码 |

### 示例

```bash
curl http://localhost:8000/extract/b275618ca10e6c62/2
```

响应结构与 `size=f` 的 `POST /extract` 相同，只是 `pages.current = page`。

### 缓存与 TTL

- 服务端把提取内容保存在**内存缓存中，TTL 30 分钟**（以 `id = md5(url)[:16]` 为 key）。
- 容器重启后缓存清空 → 需要重新 `POST /extract`。
- 对过期或未知的 `id` → `404`：
  ```json
  {"detail": "id не найден или просрочен (TTL 30 мин). Повторите POST /extract."}
  ```

### 错误码

| 码 | 场景 |
|---|---|
| `200` | 成功 |
| `404` | `id` 未找到（或 `page` 超过 `total`） |
| `422` | 路径参数非法 |

---

## `GET /health`

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"searxng-tavily-adapter","version":"2.0.0"}
```

Docker Compose 每 30 秒用此端点做健康检查。

---

## 行为与约定

- **`/search` score 是伪造的** (`0.9 - i*0.05`)。不要用它做排序。
- **`/extract` → 通过 trafilatura 输出 markdown**：保留标题、列表、表格、链接。导航/页眉/页脚/广告自动剥离。
- **超时：**
  - `/search` → SearXNG: 30 秒（硬编码）
  - `/extract` → 抓取 URL: `config.yaml` 中的 `adapter.scraper.timeout`（默认 10 秒）
- **`/search` 与抓取的并行** (`include_raw_content=true`) — 所有 URL 通过 `asyncio.gather` 并行下载。
- **`/extract` 不是批量端点** — 一次只接收一个 URL。需要多个 URL 时，请多次调用。

---

## 智能体使用模式

### 1. 快速搜索 + 摘要

```bash
curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"什么是 RAG","max_results":5}' \
  | jq '.results[] | {title, url, content}'
```

### 2. 搜索 + 快速阅读首条结果

```bash
URL=$(curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"architecture decision records","max_results":1}' \
  | jq -r '.results[0].url')

curl -sX POST localhost:8000/extract -H 'Content-Type: application/json' \
  -d "{\"url\":\"$URL\",\"size\":\"m\"}" \
  | jq -r '.content'
```

### 3. 深度阅读长文章（分页）

```bash
# 第一页
RESP=$(curl -sX POST localhost:8000/extract -H 'Content-Type: application/json' \
  -d '{"url":"https://en.wikipedia.org/wiki/Linux","size":"f"}')

echo "$RESP" | jq -r '.content'
ID=$(echo "$RESP" | jq -r '.id')
TOTAL=$(echo "$RESP" | jq -r '.pages.total')

# 剩余页
for P in $(seq 2 $TOTAL); do
  curl -s localhost:8000/extract/$ID/$P | jq -r '.content'
done
```

### 4. 搜索新闻 + 阅读最佳结果

```bash
curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"GPT-5 release","max_results":3,"categories":"news","engines":"duckduckgo,brave"}'
```
