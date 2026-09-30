#!/usr/bin/env python3
"""Call the Searcharvester /search endpoint and print results as JSON."""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error


def _headers() -> dict:
    """The job id lets the adapter's loop guard count and dedupe calls of all agents
    of one research together (set by the orchestrator)."""
    h = {"Content-Type": "application/json", "X-Searcharvester-Client": "1"}
    if os.environ.get("SEARCHARVESTER_JOB_ID"):
        h["X-Searcharvester-Job"] = os.environ["SEARCHARVESTER_JOB_ID"]
    return h


def main() -> int:
    p = argparse.ArgumentParser(description="Search via Searcharvester")
    p.add_argument("--query", required=True)
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--engines", default=None, help="comma-separated, e.g. google,duckduckgo,brave")
    p.add_argument("--categories", default=None, help="general|news|images|videos|...")
    p.add_argument(
        "--base-url",
        default=os.environ.get("SEARCHARVESTER_URL", "http://tavily-adapter:8000"),
    )
    args = p.parse_args()

    payload: dict = {
        "query": args.query,
        "max_results": args.max_results,
        "include_raw_content": False,
    }
    if args.engines:
        payload["engines"] = args.engines
    if args.categories:
        payload["categories"] = args.categories

    req = urllib.request.Request(
        f"{args.base_url.rstrip('/')}/search",
        data=json.dumps(payload).encode("utf-8"),
        headers=_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", "replace")}))
        return 1
    except Exception as e:
        print(json.dumps({"error": type(e).__name__, "detail": str(e)}))
        return 1

    # Compact view: drop the Tavily fluff fields, keep what the agent needs.
    out = {
        "query": data.get("query"),
        "results": [
            {"url": r.get("url"), "title": r.get("title"), "content": r.get("content"),
             # images/videos: the picture, its preview, a video's length
             **{k: r[k] for k in ("img_src", "thumbnail", "duration") if r.get(k)}}
            for r in data.get("results", [])
            if r.get("url")
        ],
    }
    if data.get("notice"):
        out["notice"] = data["notice"]  # from the research's loop guard
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
