"""SimpleQA through the research agent: accuracy and speed.

SimpleQA (OpenAI, 4326 short fact questions with one gold answer each) is the
usual yardstick for search agents. For a fixed random sample we run every question
through /research, take the agent's short answer and grade it with the official
SimpleQA grader prompt (CORRECT / INCORRECT / NOT_ATTEMPTED) on an LLM judge.

  python3 bench/simpleqa.py --n 30 --parallel 30 --depth quick --out bench/simpleqa-qwen36nr.jsonl
  python3 bench/simpleqa.py --report bench/simpleqa-qwen36nr.jsonl

Judge: JUDGE_MODEL (default gpt-oss-120b) at JUDGE_BASE_URL with JUDGE_API_KEY
(defaults: OPENAI_BASE_URL / OPENAI_API_KEY from the environment).
Resumable: questions already in --out are skipped.
"""

import argparse
import csv
import json
import os
import random
import re
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ANSWER_SUFFIX = (
    "\n\nThis is a short factual question. Research it, then put the final short answer "
    "on the FIRST line of report.md exactly as: ANSWER: <answer>. If you could not find "
    "it reliably, write ANSWER: I don't know."
)

# The grader template from openai/simple-evals (simpleqa_eval.py), examples shortened.
GRADER = """Your job is to look at a question, a gold target, and a predicted answer, and then assign a grade of either ["CORRECT", "INCORRECT", "NOT_ATTEMPTED"].

The following are examples of CORRECT predicted answers: the predicted answer fully contains the important information in the gold target, contains no information that contradicts it, and hedging is fine as long as the gold target is fully included and nothing contradicts it. Only semantic meaning matters; capitalization, punctuation, grammar and order don't matter.

The following are examples of INCORRECT predicted answers: a factual statement in the answer contradicts the gold target. Incorrect statements that have some hedging (e.g., "it is possible that", "although i'm not sure, i think") are also considered incorrect. Naming several candidates, one of which is right, is incorrect.

The following are examples of NOT_ATTEMPTED predicted answers: the important information in the gold target is not included in the answer, and no statements in the answer contradict the gold target ("I don't know", "I need more context", a description of what would be needed).

Also note:
- For numbers, the predicted answer must be correct to the last significant figure in the gold answer (gold 120k: "120k"/"124k"/"115k" correct, "100k" incorrect, "around 100k" not attempted).
- The gold target may contain more information than the question; the predicted answer only needs the information asked for.
- Do not punish predicted answers that omit information which is clearly inferred from the question.
- Do not punish typos in people's names if it is clearly the same name.

Here is a new example. Simply reply with either CORRECT, INCORRECT, NOT ATTEMPTED. Don't apologize or correct yourself if there was a mistake; we are just trying to grade the answer.
```
Question: {question}
Gold target: {target}
Predicted answer: {predicted}
```

Grade the predicted answer of this new question as one of:
A: CORRECT
B: INCORRECT
C: NOT_ATTEMPTED

Just return the letters "A", "B", or "C", with no text around it."""


def http(url, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"content-type": "application/json", "x-searcharvester-client": "1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def load_sample(path: Path, n: int, seed: int) -> list[dict]:
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for i, r in enumerate(rows):
        r["idx"] = i
    random.Random(seed).shuffle(rows)
    return rows[:n]


def short_answer(report: str) -> str:
    m = re.search(r"^\s*\**ANSWER\**\s*[:：]\s*(.+)$", report or "", re.M | re.I)
    if m:
        return m.group(1).strip().strip("*").strip()
    return (report or "").strip()[:600]


def grade(question: str, target: str, predicted: str) -> str:
    base = os.environ.get("JUDGE_BASE_URL", os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
    key = os.environ.get("JUDGE_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    model = os.environ.get("JUDGE_MODEL", "gpt-oss-120b")
    prompt = GRADER.format(question=question, target=target, predicted=predicted or "(no answer)")
    for attempt in range(4):
        try:
            d = http(f"{base}/chat/completions", {"model": model, "temperature": 0,
                                                   "messages": [{"role": "user", "content": prompt}]},
                     {"authorization": f"Bearer {key}"}, timeout=120)
            text = (d["choices"][0]["message"].get("content") or "").strip()
            m = re.search(r"\b([ABC])\b", text)
            if m:
                return {"A": "CORRECT", "B": "INCORRECT", "C": "NOT_ATTEMPTED"}[m.group(1)]
        except Exception:
            time.sleep(5 * (attempt + 1))
    return "GRADER_ERROR"


def run_one(adapter: str, jobs_dir: Path, row: dict, timeout: int, depth: str = "deep") -> dict:
    t0 = time.time()
    job_id = http(f"{adapter}/research", {"query": row["problem"] + ANSWER_SUFFIX, "depth": depth})["job_id"]
    st = {}
    while time.time() - t0 < timeout:
        st = http(f"{adapter}/research/{job_id}")
        if st.get("status") in ("completed", "failed", "timeout", "cancelled"):
            break
        time.sleep(5)
    wall = round(time.time() - t0, 1)
    report = st.get("report") or ""
    rp = jobs_dir / job_id / "report.md"
    if not report and rp.exists():
        report = rp.read_text(errors="replace")
    done = {}
    ev = next((q for q in (jobs_dir.parent / "state" / job_id / "events.jsonl", jobs_dir / job_id / "events.jsonl") if q.exists()), jobs_dir / job_id / "events.jsonl")
    tools = {}
    if ev.exists():
        for line in ev.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "done":
                done = e.get("payload") or {}
            if e.get("type") == "tool_call":
                t = json.dumps(e.get("payload"), ensure_ascii=False)
                k = ("search" if "search.py" in t else "extract" if "extract.py" in t
                     else "delegate" if "delegate_task" in t else "other")
                tools[k] = tools.get(k, 0) + 1
    log = jobs_dir / job_id / "hermes.log"
    builtin_web = 0
    if log.exists():
        builtin_web = len(re.findall(r"tool (web_search|web_extract|browser_\w+) ", log.read_text(errors="replace")))
    predicted = short_answer(report)
    return {
        "idx": row["idx"], "question": row["problem"], "target": row["answer"],
        "topic": eval(row["metadata"]).get("topic") if row.get("metadata") else None,
        "job_id": job_id, "depth": depth, "status": st.get("status"), "wall_s": wall, "predicted": predicted,
        "grade": grade(row["problem"], row["answer"], predicted),
        "guard": done.get("guard"), "stopped_by_guard": done.get("stopped_by_guard"),
        "lead_tools": tools, "builtin_web_calls": builtin_web,
    }


def report(path: Path) -> None:
    rows = [json.loads(l) for l in open(path) if l.strip()]
    n = len(rows)
    c = sum(r["grade"] == "CORRECT" for r in rows)
    i = sum(r["grade"] == "INCORRECT" for r in rows)
    na = n - c - i
    acc = c / n if n else 0
    cga = c / (c + i) if c + i else 0
    f1 = 2 * acc * cga / (acc + cga) if acc + cga else 0
    se = (acc * (1 - acc) / n) ** 0.5 if n else 0
    walls = [r["wall_s"] for r in rows]
    g = [r["guard"] for r in rows if r.get("guard")]
    def med(key):
        vals = [x.get(key, 0) for x in g]
        return statistics.median(vals) if vals else 0
    print(f"n={n}  correct={c}  incorrect={i}  not_attempted={na}")
    print(f"accuracy={acc:.1%} ±{1.96*se:.1%}  correct_given_attempted={cga:.1%}  F1={f1:.1%}")
    print(f"wall: median={statistics.median(walls):.0f}s  p90={sorted(walls)[int(0.9*(n-1))]:.0f}s  "
          f"max={max(walls):.0f}s  total={sum(walls)/60:.0f} min")
    print(f"per question (median): llm_calls={med('llm_calls')}  input_tokens={med('input_tokens')}  "
          f"output_tokens={med('output_tokens')}  searches={med('searches')}  extracts={med('extracts')}")
    print(f"status: {dict((s, sum(r['status']==s for r in rows)) for s in {r['status'] for r in rows})}  "
          f"stopped_by_guard={sum(bool(r.get('stopped_by_guard')) for r in rows)}  "
          f"builtin_web_calls={sum(r.get('builtin_web_calls', 0) for r in rows)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default="http://localhost:8010")
    ap.add_argument("--jobs-dir", default="./jobs")
    ap.add_argument("--data", default="bench/data/simple_qa_test_set.csv")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=1500)
    ap.add_argument("--out", default="bench/simpleqa.jsonl")
    ap.add_argument("--depth", choices=["quick", "deep"], default="quick")
    ap.add_argument("--report", default=None, help="only print the summary of this jsonl")
    args = ap.parse_args()
    if args.report:
        return report(Path(args.report))
    done = set()
    if Path(args.out).exists():
        done = {json.loads(l)["idx"] for l in open(args.out) if l.strip()}
    todo = [r for r in load_sample(Path(args.data), args.n, args.seed) if r["idx"] not in done]
    with ThreadPoolExecutor(args.parallel) as pool, open(args.out, "a") as out:
        for row in pool.map(lambda r: run_one(args.adapter, Path(args.jobs_dir), r, args.timeout, args.depth), todo):
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            print(f"{row['grade']:<14} {row['wall_s']:>6}s  {row['predicted'][:50]!r:<54} gold={row['target'][:40]!r}",
                  flush=True)
    report(Path(args.out))


if __name__ == "__main__":
    main()
