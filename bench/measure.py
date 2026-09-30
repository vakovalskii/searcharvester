"""Run /research jobs and count what one research actually costs.

For every query: start a job, wait for it, then read jobs/<id>/events.jsonl and
hermes.log and report
- wall time, final status, report size and unique URLs in the report;
- sub-agents spawned, tool calls by kind (search.py, extract.py, shell reads of
  saved extracts, other), LLM turns seen as thought/message events;
- rate-limit and retry traces (429, "rate limit", "retry") in hermes.log.

  python3 bench/measure.py --adapter http://localhost:8010 --jobs-dir ./jobs \
      --queries bench/measure_queries.txt --parallel 1 --out bench/measure.jsonl

--parallel N starts N jobs at once: that is where 429 and queueing show up.
"""

import argparse
import json
import re
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
RATE_RE = re.compile(r"\b429\b|rate.?limit|too many requests|retry(ing)? in|max_parallel", re.I)


def post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json", "x-searcharvester-client": "1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def tool_kind(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if "delegate_task" in text:
        return "delegate"
    if "search.py" in text:
        return "search"
    if "extract.py" in text:
        return "extract"
    if re.search(r"\b(grep|head|sed|tail|cat|wc)\b", text):
        return "read_file"
    return "other"


def summarize(job_dir: Path) -> dict:
    events = []
    ev_path = next((q for q in (job_dir.parent.parent / "state" / job_dir.name / "events.jsonl", job_dir / "events.jsonl") if q.exists()), job_dir / "events.jsonl")
    if ev_path.exists():
        for line in ev_path.read_text(errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    types = Counter(e.get("type") for e in events)
    agents = {e.get("agent_id") for e in events if e.get("agent_id")}
    tools = Counter(tool_kind(e.get("payload") or {}) for e in events if e.get("type") == "tool_call")
    per_agent = Counter(e.get("agent_id") for e in events if e.get("type") == "tool_call")
    log = (job_dir / "hermes.log").read_text(errors="replace") if (job_dir / "hermes.log").exists() else ""
    report = (job_dir / "report.md").read_text(errors="replace") if (job_dir / "report.md").exists() else ""
    extracts = list((job_dir / "extracts").glob("*.md")) if (job_dir / "extracts").exists() else []
    return {
        "events": len(events), "types": dict(types), "agents": len(agents),
        "tool_calls": dict(tools), "tool_calls_total": sum(tools.values()),
        "max_tool_calls_one_agent": max(per_agent.values()) if per_agent else 0,
        "extract_files": len(extracts),
        "report_chars": len(report), "report_urls": len(set(URL_RE.findall(report))),
        "rate_limit_lines": len(RATE_RE.findall(log)),
    }


def run_one(adapter: str, jobs_dir: Path, query: str, timeout: int) -> dict:
    t0 = time.time()
    job_id = post(f"{adapter}/research", {"query": query})["job_id"]
    status = "unknown"
    while time.time() - t0 < timeout:
        st = get(f"{adapter}/research/{job_id}")
        status = st.get("status")
        if status in ("completed", "failed", "timeout", "cancelled"):
            break
        time.sleep(5)
    row = {"query": query, "job_id": job_id, "status": status, "wall_s": round(time.time() - t0, 1)}
    row.update(summarize(jobs_dir / job_id))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default="http://localhost:8010")
    ap.add_argument("--jobs-dir", default="./jobs")
    ap.add_argument("--queries", required=True)
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=1500)
    ap.add_argument("--out", default="bench/measure.jsonl")
    args = ap.parse_args()
    queries = [q.strip() for q in open(args.queries) if q.strip() and not q.startswith("#")]
    with ThreadPoolExecutor(args.parallel) as pool, open(args.out, "a") as out:
        futs = [pool.submit(run_one, args.adapter, Path(args.jobs_dir), q, args.timeout) for q in queries]
        for f in futs:
            row = f.result()
            row["parallel"] = args.parallel
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            print(json.dumps({k: row[k] for k in ("status", "wall_s", "agents", "tool_calls", "tool_calls_total",
                                                  "report_urls", "rate_limit_lines")}, ensure_ascii=False),
                  "|", row["query"][:60], flush=True)


if __name__ == "__main__":
    main()
