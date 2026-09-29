"""guard.py: the loop guard of one research job (lead and sub-agents together)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from guard import JobGuard, Limits
from orchestrator import Job, Orchestrator


def g(**over) -> JobGuard:
    return JobGuard(limits=Limits(**over))


API = "2026-09-29 10:05:07 [INFO] agent.conversation_loop: API call #4: model=m provider=custom in={i} out={o} total=1 latency=2.3s id=x"
ERR = ('2026-09-29 10:05:48 [WARNING] agent.tool_executor: Tool write_file returned error (0.01s): '
       '{{"error": "Write denied: \'/srv/jobs/{n}/report.md\' is outside the root"}}')


def test_llm_call_budget_warns_then_stops():
    guard = g(max_llm_calls=5, warn_ratio=0.8)
    signals = [s for _ in range(5) for s in guard.on_log_line(API.format(i=100, o=10))]
    warns = guard.pending_warnings()
    assert [w.level for w in warns] == ["warn"]
    assert [s.level for s in signals] == ["stop"]
    assert guard.tripped and "llm_calls" in guard.tripped.reason
    assert guard.stats()["input_tokens"] == 500


def test_input_token_budget_stops():
    guard = g(max_input_tokens=50_000)
    assert not guard.on_log_line(API.format(i=30_000, o=1))
    assert guard.on_log_line(API.format(i=30_000, o=1))[0].level == "stop"


def test_same_error_with_different_numbers_counts_as_one_and_stops_once():
    guard = g(same_error_stop=3)
    out = [s for n in range(5) for s in guard.on_log_line(ERR.format(n=f"job{n}"))]
    assert [s.level for s in out] == ["stop"]


def test_different_errors_do_not_stop():
    guard = g(same_error_stop=3)
    for tool in ("a", "b", "c", "d"):
        assert not guard.on_log_line(f"Tool {tool} returned error (0.1s): boom")


def test_looping_text_stops():
    guard = g(repeat_line_stop=3)
    line = "I will now search for the latest information about the topic.\n"
    assert not guard.on_message(line) and not guard.on_message(line)
    assert guard.on_message(line)[0].level == "stop"


def test_normal_text_does_not_stop():
    guard = g(repeat_line_stop=3)
    for i in range(20):
        assert not guard.on_message(f"Finding {i}: a distinct sentence about source number {i} and its claim.\n")


def test_repeated_query_is_served_from_cache_and_not_counted():
    guard = g(max_searches=10)
    assert guard.on_search("vLLM vs SGLang MoE")[0] == "ok"
    guard.remember_search("vLLM vs SGLang MoE", {"query": "q", "results": [{"url": "u"}]})
    verdict, notice, cached = guard.on_search("  vllm VS sglang, MoE ")
    assert verdict == "duplicate" and cached["results"] == [{"url": "u"}] and notice
    assert guard.searches == 1 and guard.duplicates == 1


def test_search_budget_is_soft():
    guard = g(max_searches=2)
    assert guard.on_search("a")[0] == "ok" and guard.on_search("b")[0] == "ok"
    verdict, notice, _ = guard.on_search("c")
    assert verdict == "exhausted" and "budget" in notice
    assert guard.tripped is None  # soft: the agent is told, the job goes on


def test_extract_dedupes_urls_and_closes_in_wrapup():
    guard = g(max_extracts=1)
    assert guard.on_extract("https://www.example.com/a/")[0] == "ok"
    assert guard.on_extract("http://example.com/a#x")[0] == "duplicate"
    assert guard.on_extract("https://example.com/b")[0] == "exhausted"
    guard.wrapup = True
    assert guard.on_extract("https://example.com/a")[0] == "exhausted"
    assert guard.on_search("anything")[0] == "exhausted"


def test_idle_stop():
    guard = g(stall_s=60)
    assert guard.check_idle(now=guard.last_activity + 30) is None
    assert guard.check_idle(now=guard.last_activity + 61).level == "stop"


# ---------- adapter endpoints ----------

@pytest.fixture
def api(monkeypatch):
    job = Job(id="job0000000000001", query="q")
    job.guard = g(max_searches=1, max_extracts=1)
    orch = SimpleNamespace(get=lambda jid: job if jid == job.id else None)
    monkeypatch.setattr(main, "orchestrator", orch)
    main._extract_cache.clear()
    return TestClient(main.app), job


def test_search_endpoint_applies_guard_only_with_job_header(api, monkeypatch):
    c, job = api
    job.guard.searches = 1  # budget already spent
    r = c.post("/search", json={"query": "x"}, headers={"X-Searcharvester-Job": job.id})
    assert r.status_code == 200 and r.json()["results"] == [] and "budget" in r.json()["notice"]


def test_extract_endpoint_refuses_over_budget(api, monkeypatch):
    c, job = api
    job.guard.extracts = 1
    r = c.post("/extract", json={"url": "https://example.com/z"}, headers={"X-Searcharvester-Job": job.id})
    assert r.status_code == 200 and r.json()["content"] == "" and r.json()["notice"]


# ---------- orchestrator ----------

def test_finalize_without_report_after_guard_is_failed(tmp_path: Path):
    orch = Orchestrator(skills=[], jobs_dir=tmp_path, env={})
    job = Job(id="j", query="q", workspace_path=tmp_path)
    job.guard.tripped = job.guard._stop("llm_calls budget reached")
    asyncio.run(orch._finalize_success(job))
    assert job.status.value == "failed" and "loop guard" in job.error


def test_finalize_short_chat_reply_is_not_a_report(tmp_path: Path):
    from events import Event
    orch = Orchestrator(skills=[], jobs_dir=tmp_path, env={})
    job = Job(id="j", query="q", workspace_path=tmp_path)
    job.events.append(Event.now(job_id="j", agent_id="lead", type="message", payload={"text": "Round 1 dispatched."}))
    asyncio.run(orch._finalize_success(job))
    assert job.status.value == "failed"


def test_finalize_with_report_records_guard_stop(tmp_path: Path):
    orch = Orchestrator(skills=[], jobs_dir=tmp_path, env={})
    (tmp_path / "report.md").write_text("# R\nhttps://a.example https://b.example")
    job = Job(id="j", query="q", workspace_path=tmp_path)
    job.guard.tripped = job.guard._stop("no activity for 500 s")
    asyncio.run(orch._finalize_success(job))
    assert job.status.value == "completed"
    assert job.events[-1].payload["stopped_by_guard"] == "no activity for 500 s"


def test_quick_limits_are_narrow():
    q = Limits.quick()
    assert q.max_searches <= 10 and q.max_extracts <= 10 and q.max_llm_calls <= 40


def test_quick_job_gets_quick_guard(tmp_path: Path):
    orch = Orchestrator(skills=[], jobs_dir=tmp_path, env={})
    orch._run = lambda *a, **k: asyncio.sleep(0)
    async def go():
        jid = await orch.spawn("q", depth="quick")
        return orch.get(jid)
    job = asyncio.run(go())
    assert job.depth == "quick" and job.guard.limits.max_searches == Limits.quick().max_searches


def test_signal_group_kills_the_job_group_and_spares_ours():
    import os
    import signal
    import subprocess
    import time as _t
    from orchestrator import _signal_group
    # A leader that leaves a grandchild behind, in its own session like a job.
    proc = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; wait"], stdout=subprocess.PIPE,
                            start_new_session=True, text=True)
    grandchild = int(proc.stdout.readline())
    _signal_group(proc, signal.SIGKILL)
    proc.wait(timeout=5)
    def gone(pid):
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] == "Z"  # killed, not yet reaped
        except FileNotFoundError:
            return True
    for _ in range(50):
        if gone(grandchild):
            break
        _t.sleep(0.1)
    else:
        raise AssertionError("grandchild survived")
    assert os.getpgid(0) != proc.pid  # we are still here


def test_queue_caps_concurrent_jobs(tmp_path: Path):
    from orchestrator import JobStatus
    orch = Orchestrator(skills=[], jobs_dir=tmp_path, env={}, max_concurrent=2)
    running, peak, release = set(), [0], asyncio.Event()

    async def fake_run(job_id, query):
        running.add(job_id)
        peak[0] = max(peak[0], len(running))
        orch.get(job_id).status = JobStatus.running
        await release.wait()
        running.discard(job_id)
        orch.get(job_id).status = JobStatus.completed

    orch._run = fake_run

    async def go():
        ids = [await orch.spawn(f"q{i}", depth="quick") for i in range(5)]
        await asyncio.sleep(0.05)
        queued = sum(orch.get(i).status == JobStatus.queued for i in ids)
        release.set()
        await asyncio.sleep(0.05)
        return queued

    assert asyncio.run(go()) == 3
    assert peak[0] == 2


def test_dead_hermes_fails_the_acp_call_at_once():
    from orchestrator import HermesExited, _race_proc

    async def go():
        async def dead():
            return -9
        async def never():
            await asyncio.sleep(3600)
        exit_task = asyncio.ensure_future(dead())
        await asyncio.sleep(0)
        with pytest.raises(HermesExited, match="out of memory"):
            await _race_proc(exit_task, never(), timeout=5)
    asyncio.run(go())
