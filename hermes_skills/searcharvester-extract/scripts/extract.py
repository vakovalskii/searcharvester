#!/usr/bin/env python3
"""Fetch a URL via Searcharvester /extract and save the FULL markdown to
a local file so sub-agents can grep / head / tail it without hitting ACP
or Hermes output-truncation caps.

Returns a compact JSON pointer:
    {
      "id": "abc123",
      "url": "https://...",
      "title": "...",
      "total_chars": 18532,
      "path": "./extracts/abc123.md",
      "preview": "first 800 chars of markdown..."
    }

Read specific sections later with standard shell tools:
    cat ./extracts/abc123.md | grep -A5 -i "record"
    head -200 ./extracts/abc123.md
    sed -n '500,800p' ./extracts/abc123.md
"""
import argparse
import hashlib
import json
import math
import os
import sys
import urllib.request
import urllib.error


PAGE_SIZE = 25_000
PREVIEW_CHARS = 800
EXTRACTS_DIR_NAME = "extracts"


def _extract_id(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:16]


def _fetch_all_pages(base: str, url: str) -> tuple[dict, str]:
    """Hit /extract with size=f then paginate /extract/{id}/{page} to
    assemble the whole markdown, no matter how long the article is.

    Returns (meta, full_markdown).
    """
    payload = {"url": url, "size": "f"}
    req = urllib.request.Request(
        f"{base}/extract",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        first = json.loads(resp.read().decode("utf-8"))

    total_pages = int(first.get("pages", {}).get("total") or 1)
    parts: list[str] = [first.get("content", "")]
    extract_id = first.get("id")

    for page in range(2, total_pages + 1):
        purl = f"{base}/extract/{extract_id}/{page}"
        req = urllib.request.Request(purl, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts.append(data.get("content", ""))
        except Exception as e:
            parts.append(f"\n[!] Failed to fetch page {page}: {e}\n")
            break

    full = "".join(parts)
    meta = {
        "id": extract_id,
        "url": first.get("url", url),
        "title": first.get("title", ""),
        "total_chars": first.get("total_chars") or len(full),
        "pages_fetched": total_pages,
        "notice": first.get("notice"),
    }
    return meta, full


def _save_extract(full_md: str, extract_id: str) -> str:
    """Write to ./extracts/{id}.md, returning the relative path."""
    out_dir = os.path.join(os.getcwd(), EXTRACTS_DIR_NAME)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{extract_id}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(full_md)
    return os.path.relpath(out_path, os.getcwd())


def _headers() -> dict:
    """The job id lets the adapter's loop guard count and dedupe calls of all agents
    of one research together (set by the orchestrator)."""
    h = {"Content-Type": "application/json", "X-Searcharvester-Client": "1"}
    if os.environ.get("SEARCHARVESTER_JOB_ID"):
        h["X-Searcharvester-Job"] = os.environ["SEARCHARVESTER_JOB_ID"]
    return h


def main() -> int:
    p = argparse.ArgumentParser(
        description="Extract a URL to ./extracts/<id>.md and return a pointer."
    )
    p.add_argument("--url", required=False, help="Page URL")
    # Kept for back-compat with older prompts; ignored — we always save full.
    p.add_argument("--size", default=None, help="(ignored) size is always 'full'")
    p.add_argument(
        "--base-url",
        default=os.environ.get("SEARCHARVESTER_URL", "http://tavily-adapter:8000"),
    )
    args = p.parse_args()

    if not args.url:
        print(json.dumps({"error": "--url is required"}))
        return 2

    base = args.base_url.rstrip("/")

    try:
        meta, full = _fetch_all_pages(base, args.url)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        print(json.dumps({"error": f"HTTP {e.code}", "detail": detail}))
        return 1
    except Exception as e:
        print(json.dumps({"error": type(e).__name__, "detail": str(e)}))
        return 1

    if meta.get("notice") and not full:
        # The research's loop guard closed page reads: tell the agent, save nothing.
        print(json.dumps({"url": args.url, "notice": meta["notice"]}, ensure_ascii=False, indent=2))
        return 0

    extract_id = meta["id"] or _extract_id(args.url)
    path = _save_extract(full, extract_id)
    preview = full[:PREVIEW_CHARS]

    print(json.dumps({
        "id": extract_id,
        "url": meta["url"],
        "title": meta["title"],
        "total_chars": meta["total_chars"],
        "pages_fetched": meta["pages_fetched"],
        "path": path,
        "preview": preview,
        "hint": (
            f"Full markdown saved to {path}. Read specific parts with: "
            f"`grep -ni 'keyword' {path}`, `head -200 {path}`, or "
            f"`sed -n '1,300p' {path}`."
        ),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
