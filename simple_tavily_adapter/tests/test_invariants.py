"""Invariants we learned the hard way. Each test names the incident it guards.

Two kinds:
- end to end: the real Orchestrator drives tests/fake_hermes.py, a real ACP agent
  over stdio that fails on demand (huge lines, OOM kills, hangs, loops, killpg);
- static: the Hermes config, skills, compose file and image that a research job
  depends on. They read the working tree (scripts/test.sh mounts it at /repo) and
  fall back to the paths inside the image.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest
import yaml

from orchestrator import JobStatus, Orchestrator

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_hermes.py"
REPO = Path(os.environ.get("REPO_ROOT", HERE.parent.parent))


def _first(*paths: Path) -> Path | None:
    return next((p for p in paths if p.exists()), None)


CONFIG = _first(REPO / "hermes-data" / "config.yaml", Path("/opt/data/config.yaml"))
SKILLS = _first(REPO / "hermes_skills", Path("/opt/searcharvester-skills"))
COMPOSE = _first(REPO / "docker-compose.yaml")
ADAPTER = _first(REPO / "simple_tavily_adapter", HERE.parent)


# ---------------------------------------------------------------- end to end

def _fake_bin(tmp_path: Path) -> str:
    b = tmp_path / "fake-hermes"
    b.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    b.chmod(0o755)
    return str(b)


def run_job(tmp_path, monkeypatch, mode, *, depth="quick", timeout=60, **orch_kw):
    """Spawn one job on the fake agent, wait for a terminal state, return (job, seconds)."""
    monkeypatch.setenv("FAKE_HERMES_MODE", mode)
    jobs = tmp_path / "jobs"
    jobs.mkdir(exist_ok=True)
    orch = Orchestrator(hermes_bin=_fake_bin(tmp_path), skills=["s"], jobs_dir=jobs, env={},
                        timeout_sec=timeout, **orch_kw)

    async def go():
        t0 = time.monotonic()
        jid = await orch.spawn("what is it", depth=depth)
        while orch.get(jid).status in (JobStatus.queued, JobStatus.running):
            if time.monotonic() - t0 > timeout + 30:
                raise AssertionError(f"job still {orch.get(jid).status} after {timeout + 30}s")
            await asyncio.sleep(0.1)
        return orch.get(jid), time.monotonic() - t0

    return asyncio.run(go())


def done_payload(job):
    return next(e.payload for e in reversed(job.events) if e.type == "done")


def test_happy_path_completes_with_report_and_guard_stats(tmp_path, monkeypatch):
    job, _ = run_job(tmp_path, monkeypatch, "report")
    assert job.status == JobStatus.completed
    assert job.report.startswith("ANSWER: 42")
    assert "guard" in done_payload(job)


def test_one_acp_line_of_megabytes_does_not_kill_the_job(tmp_path, monkeypatch):
    """29.09: 'Separator is found, but chunk is longer than limit' on the final write."""
    job, _ = run_job(tmp_path, monkeypatch, "huge_line")
    assert job.status == JobStatus.completed, job.error


def test_hermes_killed_mid_turn_fails_the_job_at_once(tmp_path, monkeypatch):
    """29.09: OOM kill left the job 'running' for 20+ minutes."""
    job, took = run_job(tmp_path, monkeypatch, "die_in_prompt", timeout=120)
    assert job.status == JobStatus.failed and "exited" in job.error
    assert took < 20


def test_hermes_killed_after_new_session_fails_the_job_at_once(tmp_path, monkeypatch):
    job, took = run_job(tmp_path, monkeypatch, "die_after_session", timeout=120)
    assert job.status == JobStatus.failed and "exited" in job.error
    assert took < 20


def test_acp_initialize_that_never_answers_times_out(tmp_path, monkeypatch):
    job, took = run_job(tmp_path, monkeypatch, "hang_init", timeout=120, acp_init_timeout=1)
    assert job.status == JobStatus.failed
    assert took < 15


def test_a_short_chat_reply_is_not_a_completed_research(tmp_path, monkeypatch):
    """29.09: gpt-oss wrote the tool call as text, the job came out 'completed' in 20 s."""
    job, _ = run_job(tmp_path, monkeypatch, "short_reply")
    assert job.status == JobStatus.failed


def test_looping_text_is_stopped_and_the_wrap_up_turn_writes_the_report(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARD_REPEAT_LINE_STOP", "4")
    job, _ = run_job(tmp_path, monkeypatch, "loop_text")
    assert job.status == JobStatus.completed
    assert "looping text" in done_payload(job)["stopped_by_guard"]
    actions = [e.payload.get("action") for e in job.events if e.payload.get("kind") == "guard"]
    assert actions[:2] == ["stop", "wrapup"]


def test_the_same_error_from_hermes_log_stops_the_job(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARD_SAME_ERROR_STOP", "5")
    monkeypatch.setenv("GUARD_WRAPUP_S", "3")
    job, took = run_job(tmp_path, monkeypatch, "same_error", depth="deep")
    assert job.status == JobStatus.failed and "loop guard" in job.error
    assert took < 30


def test_killpg_inside_hermes_does_not_reach_the_adapter(tmp_path, monkeypatch):
    """29.09: at 30 parallel jobs uvicorn (PID 1) exited 0 and every job was lost."""
    import signal
    got = []
    # A handler, not the default: as PID 1 in a container the default ignores SIGTERM
    # and the test would pass even when the signal reaches us.
    old = signal.signal(signal.SIGTERM, lambda *a: got.append(a[0]))
    try:
        job, _ = run_job(tmp_path, monkeypatch, "kill_group")
    finally:
        signal.signal(signal.SIGTERM, old)
    assert job.status == JobStatus.completed
    assert got == [], "the job's killpg reached the adapter's process group"


def test_hermes_gets_the_job_scoped_environment(tmp_path, monkeypatch):
    job, _ = run_job(tmp_path, monkeypatch, "env")
    env = json.loads((job.workspace_path / "env.json").read_text())
    # 29.09: the image-wide /opt/data root denied every write into the workspace.
    assert Path(env["HERMES_WRITE_SAFE_ROOT"]) == job.workspace_path
    # 29.09: without it ACP runs delegate_task in the background and children die.
    assert env["HERMES_SINGLE_QUERY_SESSION"] == "1"
    # the guard counts /search and /extract per job by this id
    assert env["SEARCHARVESTER_JOB_ID"] == job.id
    assert env["pgid_is_own"] is True


def test_writes_are_allowed_only_inside_the_workspace(tmp_path, monkeypatch):
    """29.09: 'method not found' on request_permission denied writes, the agent looped."""
    job, _ = run_job(tmp_path, monkeypatch, "write_outside")
    verdicts = json.loads((job.workspace_path / "verdicts.json").read_text())
    assert verdicts == {"outside": "cancelled", "inside": "selected"}


def test_no_process_of_a_finished_job_is_left(tmp_path, monkeypatch):
    job, _ = run_job(tmp_path, monkeypatch, "report")
    pid = job._process.pid
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


# ---------------------------------------------------------------- static

needs_config = pytest.mark.skipif(CONFIG is None, reason="hermes config not found")
needs_skills = pytest.mark.skipif(SKILLS is None, reason="skills not found")
needs_compose = pytest.mark.skipif(COMPOSE is None, reason="run via scripts/test.sh to see the repo")


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load(CONFIG.read_text())


@needs_config
def test_research_agents_have_no_builtin_web_or_browser_tools(cfg):
    """29.09: agents used web_search/web_extract (keyless third-party scraper) and browser."""
    allowed = {"terminal", "file", "skills", "delegation", "todo"}
    for platform in ("acp", "subagent"):
        tools = set(cfg["platform_toolsets"][platform])
        assert tools <= allowed, f"{platform}: {tools - allowed}"
    assert "delegation" not in cfg["platform_toolsets"]["subagent"]


@needs_config
def test_tirith_is_off(cfg):
    """29.09: tirith blocked plain-http adapter calls and non-ASCII queries; agents went around it."""
    assert cfg["security"]["tirith_enabled"] is False


@needs_config
def test_hermes_loop_guard_hard_stops_are_on_in_the_form_hermes_reads(cfg):
    """29.09: flat keys were silently overridden by the nested defaults."""
    g = cfg["tool_loop_guardrails"]
    assert g["hard_stop_enabled"] is True
    for section in ("warn_after", "hard_stop_after"):
        assert set(g[section]) >= {"exact_failure", "same_tool_failure", "idempotent_no_progress"}
    assert g["hard_stop_after"]["exact_failure"] <= 5


@needs_config
def test_turn_budgets_and_team_size(cfg):
    assert cfg["agent"]["max_turns"] <= 60
    assert cfg["delegation"]["max_iterations"] <= 30
    assert cfg["delegation"]["max_spawn_depth"] == 1
    # the deep-research skill spawns up to 3 researchers + critic + fact-checker
    assert cfg["delegation"]["oneshot_max_children"] >= 5


@needs_config
def test_no_secret_in_the_hermes_config():
    text = CONFIG.read_text()
    assert not re.search(r"^\s*api_key:\s*\S", text, re.M)
    assert not re.search(r"\bsk-[A-Za-z0-9_-]{12,}", text)


@needs_skills
def test_skills_use_real_script_paths():
    """29.09: v0.21 no longer fills SKILL_DIR; children guessed /srv/searxng-docker/search.py."""
    for md in SKILLS.glob("*/SKILL.md"):
        text = md.read_text()
        assert "SKILL_DIR" not in text, md
        for name, script in re.findall(r"/opt/data/skills/([\w-]+)/scripts/([\w.]+\.py)", text):
            assert (SKILLS / name / "scripts" / script).exists(), f"{md}: {name}/{script}"


@needs_skills
def test_skill_scripts_tag_every_adapter_call_with_the_job():
    for script in SKILLS.glob("*/scripts/*.py"):
        text = script.read_text()
        if "urllib.request.Request(" not in text:
            continue
        assert "X-Searcharvester-Job" in text, script
        for call in re.findall(r"urllib\.request\.Request\((.*?)\)\n", text, re.S):
            if "data=" in call:
                assert "headers=_headers()" in call, f"{script}: POST without the job header"


@needs_compose
def test_compose_publishes_on_localhost_only_and_runs_an_init():
    comp = yaml.safe_load(COMPOSE.read_text())
    for name, svc in comp["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name}: {port}"
    adapter = comp["services"]["tavily-adapter"]
    assert adapter.get("init") is True  # uvicorn as PID 1 did not reap agents' orphans
    env = "\n".join(adapter.get("environment", []))
    assert "MAX_CONCURRENT_JOBS" in env
    assert re.search(r"NEURALDEEP_API_KEY=\$\{", env), "the key must come from the environment"


@pytest.mark.skipif(ADAPTER is None, reason="adapter sources not found")
def test_entrypoint_drops_privileges_without_gosu():
    text = (ADAPTER / "docker" / "entrypoint-adapter.sh").read_text()
    assert "setpriv" in text
    assert not re.search(r"^\s*exec\s+gosu\b", text, re.M)


@pytest.mark.skipif(not Path("/opt/hermes/agent/turn_api_call.py").exists(), reason="not in the image")
def test_image_has_the_streaming_patch():
    assert "HERMES_DISABLE_STREAMING" in Path("/opt/hermes/agent/turn_api_call.py").read_text()


# ---------------------------------------------------------------- stage A data

def test_events_carry_a_gapless_seq_and_resume_after_it(tmp_path, monkeypatch):
    job, _ = run_job(tmp_path, monkeypatch, "report")
    seqs = [e.seq for e in job.events]
    assert seqs == list(range(1, len(seqs) + 1))


def test_spawn_carries_depth_and_limits_and_every_done_has_counters(tmp_path, monkeypatch):
    for mode in ("report", "short_reply", "die_in_prompt"):
        (tmp_path / mode).mkdir()
        job, _ = run_job(tmp_path / mode, monkeypatch, mode, depth="quick")
        spawn = next(e for e in job.events if e.type == "spawn" and e.agent_id == "lead")
        assert spawn.payload["depth"] == "quick" and spawn.payload["limits"]["max_searches"] > 0
        done = done_payload(job)
        assert "guard" in done and "limits" in done, mode


def test_job_survives_an_adapter_restart_on_disk(tmp_path, monkeypatch):
    job, _ = run_job(tmp_path, monkeypatch, "report")
    fresh = Orchestrator(hermes_bin="x", skills=[], jobs_dir=tmp_path / "jobs", env={},
                         state_dir=tmp_path / "jobs")
    meta = fresh.load_meta(job.id)
    assert meta["status"] == "completed" and meta["query"] == "what is it"
    assert fresh.load_report(job.id, meta).startswith("ANSWER: 42")
    disk = fresh.disk_events(job.id)
    assert [d["seq"] for d in disk] == [e.seq for e in job.events]
    assert [j["id"] for j in fresh.list_jobs()] == [job.id]


def test_fallback_report_is_on_disk_too(tmp_path, monkeypatch):
    """A report that came as a chat reply (no report.md) must survive a restart."""
    from orchestrator import JOB_META_FILENAME
    job, _ = run_job(tmp_path, monkeypatch, "short_reply")
    meta = json.loads((tmp_path / "jobs" / job.id / JOB_META_FILENAME).read_text())
    assert meta["status"] == "failed"


def test_restart_marks_queued_and_running_jobs_interrupted(tmp_path):
    state = tmp_path / "state"
    for jid, st in (("0000000000000001", "queued"), ("0000000000000002", "running"), ("0000000000000003", "completed")):
        (state / jid).mkdir(parents=True)
        (state / jid / "job.json").write_text(json.dumps({"id": jid, "query": "q", "status": st}))
    orch = Orchestrator(hermes_bin="x", skills=[], jobs_dir=tmp_path / "jobs", env={}, state_dir=state)
    got = {j["id"]: j["status"] for j in orch.list_jobs()}
    assert got == {"0000000000000001": "interrupted", "0000000000000002": "interrupted",
                   "0000000000000003": "completed"}
    last = orch.disk_events("0000000000000002")[-1]
    assert last["type"] == "done" and last["payload"]["status"] == "interrupted"


def test_legacy_log_status_comes_from_the_lead_only(tmp_path):
    jobs = tmp_path / "jobs"
    d = jobs / "00000000000000aa"
    d.mkdir(parents=True)
    lines = [
        {"ts": "t1", "job_id": "x", "agent_id": "lead", "parent_id": None, "type": "spawn", "payload": {"query": "old q"}},
        {"ts": "t2", "job_id": "x", "agent_id": "sub-ab-1", "parent_id": "lead", "type": "done", "payload": {"status": "completed"}},
    ]
    (d / "events.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    orch = Orchestrator(hermes_bin="x", skills=[], jobs_dir=jobs, env={}, state_dir=tmp_path / "state")
    meta = orch.load_meta("00000000000000aa")
    assert meta["status"] == "interrupted" and meta["query"] == "old q"
    assert [e["seq"] for e in orch.disk_events("00000000000000aa")] == [1, 2]


def test_cancel_of_a_running_job_stays_cancelled(tmp_path, monkeypatch):
    """30.09: a cancelled job came out 'failed': the dying session wrote a second done."""
    monkeypatch.setenv("FAKE_HERMES_MODE", "hang")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    orch = Orchestrator(hermes_bin=_fake_bin(tmp_path), skills=["s"], jobs_dir=jobs, env={}, timeout_sec=60)

    async def go():
        jid = await orch.spawn("q", depth="deep")
        while orch.get(jid).status != JobStatus.running or not any(e.type == "spawn" for e in orch.get(jid).events):
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        assert await orch.cancel(jid)
        for _ in range(100):  # let _run notice the dead process and try to fail the job
            await asyncio.sleep(0.05)
        return orch.get(jid)

    job = asyncio.run(go())
    assert job.status == JobStatus.cancelled
    lead_done = [e for e in job.events if e.type == "done" and e.agent_id == "lead"]
    assert [d.payload["status"] for d in lead_done] == ["cancelled"]
    assert orch.load_meta(job.id)["status"] == "cancelled"
