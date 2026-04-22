# API reference

Base URL: `http://localhost:8000` (or wherever you published port `8000` of the `tavily-adapter` container).

No auth. Any `api_key` in requests is ignored — it's accepted only for compatibility with Tavily clients.

Endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/search` | Search: links + snippets (+ optional markdown `raw_content`) |
| `POST` | `/extract` | Extract a page as markdown (s/m/l/f + pagination) |
| `GET` | `/extract/{id}/{page}` | Next page of extracted content (only for `size=f`) |
| `GET` | `/health` | Health-check |

---

## `POST /search`

Tavily-compatible search. Takes JSON, returns JSON.

### Request

| Field | Type | Default | Description |
|---|---|---|---|
| `query` | string | — | required, search query text |
| `max_results` | int | `10` | how many results to return |
| `include_raw_content` | bool | `false` | `true` → fetch pages and put markdown into `raw_content` (truncated at `adapter.scraper.max_content_length`) |
| `engines` | string \| null | `"google,duckduckgo,brave"` | comma-separated SearXNG engines. Examples: `"google"`, `"duckduckgo,brave"`, `"yandex,mojeek"` |
| `categories` | string \| null | `"general"` | SearXNG category: `general`, `news`, `images`, `videos`, `map`, `music`, `it`, `science`, `files`, `social` |

Example:

```bash
curl -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "latest AI news",
    "max_results": 5,
    "engines": "duckduckgo,brave",
    "categories": "news"
  }'
```

### Response

Tavily schema (kept for backward compatibility):

```json
{
  "query": "latest AI news",
  "follow_up_questions": null,
  "answer": null,
  "images": [],
  "results": [
    {
      "url": "https://example.com/article",
      "title": "AI Breakthrough 2026",
      "content": "Short snippet from the search engine...",
      "score": 0.9,
      "raw_content": "# AI Breakthrough 2026\n\nMarkdown content..."
    }
  ],
  "response_time": 1.42,
  "request_id": "uuid"
}
```

**`raw_content`** is now returned as **markdown** (via trafilatura) — with headings, lists, links. It used to be plain text.

### Error codes

| Code | When |
|---|---|
| `200` | ok (even if `results` is empty) |
| `422` | invalid JSON / missing `query` |
| `500` | SearXNG failed |
| `504` | SearXNG didn't respond in 30 seconds |

---

## `POST /extract`

Fetches a page, extracts main content (trafilatura), returns markdown.

### Request

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | — | required, page URL |
| `size` | `"s"` \| `"m"` \| `"l"` \| `"f"` | `"m"` | size preset (see below) |

**Size presets:**

| Value | Behavior | When to use |
|---|---|---|
| `s` | trim to **5,000** chars | short summary for LLMs with small context |
| `m` | trim to **10,000** chars | regular size for agent reading |
| `l` | trim to **25,000** chars | deep reading of an article |
| `f` | **full** content, paged at 25,000 chars | when you need the whole document |

### Response (size ∈ s/m/l)

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

- `chars` — length of `content` in this response.
- `total_chars` — full length of extracted content **before** trimming (useful for knowing whether you lost context).
- For `s/m/l` there's no pagination — `pages.total: 1`.

### Response (size = f)

When content fits into a single page (≤ 25,000 chars):

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

When content is longer than 25,000 chars:

```json
{
  "id": "b275618ca10e6c62",
  "url": "...",
  "title": "...",
  "format": "md",
  "size": "f",
  "content": "first 25000 chars of markdown...",
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

The `pages.next` field contains a ready-to-use path for the next `GET` request.

### Error codes

| Code | When |
|---|---|
| `200` | ok |
| `422` | page has no extractable content (trafilatura returned empty) |
| `502` | couldn't fetch URL (non-200 response / network error) |

---

## `GET /extract/{id}/{page}`

Returns a page of previously extracted content. Only works for documents originally requested with `size=f`.

### Path parameters

| Param | Type | Description |
|---|---|---|
| `id` | string (16 hex) | `id` from the previous `POST /extract` |
| `page` | int ≥ 1 | page number |

### Example

```bash
curl http://localhost:8000/extract/b275618ca10e6c62/2
```

Response has the same shape as `POST /extract` with `size=f`, but with `pages.current = page`.

### Cache and TTL

- The server keeps extracted content in an **in-memory cache for 30 minutes** (keyed by `id = md5(url)[:16]`).
- After a container restart the cache is wiped → you'll need another `POST /extract`.
- For an expired / unknown `id` → `404`:
  ```json
  {"detail": "id not found or expired (TTL 30 min). Retry POST /extract."}
  ```

### Error codes

| Code | When |
|---|---|
| `200` | ok |
| `404` | `id` not found (or `page` > `total`) |
| `422` | invalid path parameters |

---

## `GET /health`

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"searxng-tavily-adapter","version":"2.0.0"}
```

Docker Compose uses this endpoint as healthcheck every 30 seconds.

---

## Behavior and guarantees

- **`/search` score is fake** (`0.9 - i*0.05`). Don't use it for ranking.
- **`/extract` → markdown via trafilatura**: keeps headings, lists, tables, links. Navigation/header/footer/ads are stripped automatically.
- **Timeouts:**
  - `/search` → SearXNG: 30s (hardcoded)
  - `/extract` → scraping URL: `adapter.scraper.timeout` from `config.yaml` (default 10s)
- **`/search` scraping parallelism** (when `include_raw_content=true`) — all URLs are fetched in parallel via `asyncio.gather`.
- **`/extract` is NOT a batch endpoint** — it takes one URL. For multiple URLs, make multiple requests.

---

## Agent usage patterns

### 1. Fast search + snippets

```bash
curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"what is RAG","max_results":5}' \
  | jq '.results[] | {title, url, content}'
```

### 2. Search + quick read of the top link

```bash
URL=$(curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"architecture decision records","max_results":1}' \
  | jq -r '.results[0].url')

curl -sX POST localhost:8000/extract -H 'Content-Type: application/json' \
  -d "{\"url\":\"$URL\",\"size\":\"m\"}" \
  | jq -r '.content'
```

### 3. Deep read of a long article (pagination)

```bash
# First page
RESP=$(curl -sX POST localhost:8000/extract -H 'Content-Type: application/json' \
  -d '{"url":"https://en.wikipedia.org/wiki/Linux","size":"f"}')

echo "$RESP" | jq -r '.content'
ID=$(echo "$RESP" | jq -r '.id')
TOTAL=$(echo "$RESP" | jq -r '.pages.total')

# Remaining pages
for P in $(seq 2 $TOTAL); do
  curl -s localhost:8000/extract/$ID/$P | jq -r '.content'
done
```

### 4. News search + read the best result

```bash
curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"GPT-5 release","max_results":3,"categories":"news","engines":"duckduckgo,brave"}'
```
