---
name: searcharvester-extract
description: >
  Fetch a web page and extract its main content as clean markdown via
  trafilatura. Strips navigation, ads, and footers automatically. Supports
  size presets (short/medium/long) and full mode with pagination for very
  long documents. Use after `searcharvester-search` to actually read the
  pages behind your URLs.
version: 1.0.0
metadata:
  hermes:
    tags: [extract, read, content, markdown, research]
    category: research
---

# Searcharvester Extract

## When to Use
- You have a specific URL and need its readable content (articles, docs, wiki pages).
- You followed up a `searcharvester-search` result and want to read it.
- You need a clean markdown version of a page for citation quoting.
- A document is too long for a single read — the `f` preset paginates it.

Do **not** use this skill for:
- Discovery of URLs (use `searcharvester-search` first).
- Non-HTML resources (PDFs, images) — the service expects HTML pages.

## Procedure

The extract endpoint lives at `$SEARCHARVESTER_URL` (default `http://tavily-adapter:8000`). Use the helper script.

### Size presets

| Preset | Chars returned | Use case |
|---|---|---|
| `s` | 5 000 | Quick summary, LLM with small context |
| `m` | 10 000 | Default — most articles |
| `l` | 25 000 | Deep reading |
| `f` | full (paginated by 25 000) | Very long docs; take additional pages via page param |

### Single page extraction

```bash
python3 /opt/data/skills/searcharvester-extract/scripts/extract.py --url "https://en.wikipedia.org/wiki/Docker_(software)" --size m
```

### Full document with pagination

```bash
# Page 1 (also returns metadata: total pages, id for follow-up pages)
python3 /opt/data/skills/searcharvester-extract/scripts/extract.py --url "https://long.example.com/article" --size f

# Fetch page 2 using id from first response
python3 /opt/data/skills/searcharvester-extract/scripts/extract.py --id b275618ca10e6c62 --page 2
```

### Output

JSON printed to stdout:

```json
{
  "id": "b275618ca10e6c62",
  "url": "...",
  "title": "Page title",
  "size": "m",
  "content": "# Markdown body\n...",
  "chars": 10000,
  "total_chars": 33430,
  "pages": {"current": 1, "total": 1, "page_size": 10000}
}
```

Use `content` directly — it's ready markdown with headings, lists, tables, links.

## Parameters reference

| Flag | Values | Default |
|---|---|---|
| `--url` | URL | required (unless `--id` given) |
| `--size` | `s`/`m`/`l`/`f` | `m` |
| `--id` | 16-char hex (from previous response) | — |
| `--page` | int ≥ 1 | 1 (use with `--id`) |

## Pitfalls

- **422 error**: trafilatura couldn't find extractable main-content (the page is almost entirely JavaScript or ads). Try another source from your search results.
- **502 error**: the site blocked us or returned an error. Try another URL.
- **Cache expired**: `/extract/{id}/{page}` returns 404 after 30 minutes. Re-run with `--url --size f` to get a fresh id.
- **`total_chars >> chars`**: your content is truncated. Either raise to `--size l`, or switch to `--size f` and paginate.

## Verification

Successful response has `content` non-empty and `chars > 0`. If you need the full document, check `total_chars` vs `chars` and decide whether to fetch more pages.
