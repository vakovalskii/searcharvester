---
name: searcharvester-search
description: >
  Search the web via SearXNG (100+ engines: Google, DuckDuckGo, Brave, Bing,
  Yandex, Mojeek, ...) through a Tavily-compatible API. Use when the user needs
  to find information, recent news, compare sources, gather facts, or locate
  specific URLs. Returns title + URL + short snippet for each result. For full
  page content, follow up with the `searcharvester-extract` skill.
version: 1.0.0
metadata:
  hermes:
    tags: [search, web, research, citations]
    category: research
---

# Searcharvester Search

## When to Use
- The user asks to find information, news, articles, or sources on a topic.
- You need citations with URLs before writing a research answer.
- The user requests comparison across multiple sources.
- You want to locate the authoritative URL for a specific fact before extracting it.

Do **not** use this skill for:
- Reading the full content of a known URL (use `searcharvester-extract` instead).
- Answering questions from your own knowledge without citations (just reply directly).

## Procedure

The search endpoint lives at `$SEARCHARVESTER_URL` (default `http://tavily-adapter:8000`). It is reachable via the helper script in this skill.

### Basic search

```bash
python3 /opt/data/skills/searcharvester-search/scripts/search.py --query "what is retrieval augmented generation" --max-results 5
```

### Pick a specific engine or category

```bash
# Only DuckDuckGo + Brave
python3 /opt/data/skills/searcharvester-search/scripts/search.py --query "GPT-5 release" --engines duckduckgo,brave --max-results 5

# News category for fresh info
python3 /opt/data/skills/searcharvester-search/scripts/search.py --query "OpenAI news today" --categories news --max-results 10
```

### Images and videos

```bash
# Pictures: diagrams, charts, photos. Each hit has img_src (the image) and thumbnail.
python3 /opt/data/skills/searcharvester-search/scripts/search.py --query "Python 3.13 free-threading benchmark chart" --categories images --max-results 8

# Videos: talks, reviews, demos. Each hit has url (the video), thumbnail and duration.
python3 /opt/data/skills/searcharvester-search/scripts/search.py --query "Python 3.13 release overview" --categories videos --max-results 6
```

Do not pass `--engines` here: the category has its own image and video engines.
A media search counts against the research budget like any other search.
Only these `img_src` / `thumbnail` addresses can be shown in a report: the UI
displays a picture only if search returned it to this research.

### Output

JSON printed to stdout:

```json
{
  "query": "...",
  "results": [
    {
      "url": "https://...",
      "title": "...",
      "content": "short snippet from the search engine",
      "img_src": "https://... (images only)",
      "thumbnail": "https://... (images and videos, when the engine has one)",
      "duration": "8:45 (videos only)"
    }
  ]
}
```

Parse it with `jq`, or read it straight if short. Save links to your plan so you can extract them next.

## Parameters reference

| Flag | Values | Default | Notes |
|---|---|---|---|
| `--query` | string | required | Free-text query |
| `--max-results` | int | 10 | How many hits to return |
| `--engines` | comma-separated | `google,duckduckgo,brave` | Any [SearXNG engine](https://docs.searxng.org/admin/engines/index.html) |
| `--categories` | `general`/`news`/`images`/`videos`/`map`/`music`/`it`/`science`/`files`/`social` | `general` | |

## Pitfalls

- **Empty results**: Google is rate-limiting — rerun with `--engines duckduckgo,brave`.
- **504 timeout**: SearXNG is probably waiting on a slow engine. Narrow `--engines`.
- **Too few good hits**: broaden the query (remove dates, use synonyms) or raise `--max-results`.

## Verification

A working call returns HTTP 200 with at least one URL in `results[]`. If `results` is `[]`, try different engines.
