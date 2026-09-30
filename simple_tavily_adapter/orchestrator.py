"""
Research job orchestrator (ACP-based).

Per /research: spawn `hermes acp` as a subprocess, connect via the Python
`acp` SDK, normalize every `session_update` into a flat Event, and expose
the event stream over SSE.

Compared to the previous docker-py + stdout-regex approach this:
  * drops the need for docker-socket-proxy (no per-job container spawn)
  * drops the jobs_host_dir vs jobs_dir path-translation gotcha
  * gives the UI typed events instead of us grepping emoji lines

Public surface intentionally stays close to the old one: spawn / get /
cancel / read_logs (compat) plus new: subscribe / events / snapshot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import uuid
from dataclasses import asdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from events import Event, normalize_acp_update
import permissions
from guard import JobGuard, Limits, Signal
from subagents import SubagentTail

logger = logging.getLogger(__name__)

REPORT_FILENAME = "report.md"
ACP_LINE_LIMIT = 32 * 1024 * 1024  # bytes per ACP message line from hermes
LOG_FILENAME = "hermes.log"
EVENTS_FILENAME = "events.jsonl"
JOB_META_FILENAME = "job.json"
TERMINAL = {"completed", "failed", "timeout", "cancelled", "interrupted"}

# Appended to every user query. Keeps the agent honest about where the final
# report lives and nudges it away from reflexive refusals on legitimate
# public-web research tasks.
QUICK_SKILLS = ["searcharvester-search", "searcharvester-extract"]


def _quick_suffix() -> str:
    """Prompt suffix for depth=quick: one agent, a narrow budget, no team."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""

---
CONTEXT — today's date is {today}. TRUST SOURCES OVER MEMORY.

INSTRUCTIONS — quick research, you work ALONE: do NOT call delegate_task.
1. Run 1–4 searches with the searcharvester-search skill (search.py).
2. Read the 1–3 most promising pages with the searcharvester-extract skill
   (extract.py) and grep the saved file for the exact fact. Fetch pages ONLY
   with extract.py: no curl, no wget, no scripts of your own.
3. Write ./report.md: the answer first, then 1–3 source URLs you actually read.
Stop as soon as one good source confirms the answer. The budget is small
(about 8 searches and 8 page reads) and tool calls past it return nothing."""


def _mandatory_suffix() -> str:
    """Prompt suffix — kept short. Defers detail to the
    searcharvester-deep-research skill."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"""

---
CONTEXT — today's date is {today}. Your training data is older than
this, so anything you "know" about records, counts, versions, or
current holders may be outdated. TRUST SOURCES OVER MEMORY.

INSTRUCTIONS — use the `searcharvester-deep-research` skill. It runs a
two-round pipeline:
  Round 1: delegate_task([researchers...])  — 2–3 researchers in parallel
  Round 2: delegate_task([critic, fact-checker])  — with the
           researchers' findings handed in as context
Then you (the lead) synthesise a cited report.

Do NOT try to answer the question yourself between rounds; your only
job is to read each round's JSON, decide what context the next round
needs, and fire the next delegate_task.

CRITICAL — when building the `context` string for EVERY sub-agent
task in EVERY delegate_task call, the FIRST line of context must be:

    Today's date is {today}. Search for sources from {today[:4]} when possible.

Sub-agents have stale training data too — without this preamble they
search for "latest 2024" news in {today[:4]} and miss everything new.

Output: `./report.md` (relative path), following the skill's format."""


# Keep the module-level constant for back-compat but it's evaluated once
# per module import; the orchestrator uses _mandatory_suffix() at send
# time so the date stays fresh on long-running containers.
MANDATORY_SUFFIX = _mandatory_suffix()


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    timeout = "timeout"
    cancelled = "cancelled"
    interrupted = "interrupted"   # the adapter restarted while the job was queued or running


@dataclass
class Job:
    id: str
    query: str
    status: JobStatus = JobStatus.queued
    workspace_path: Path | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_sec: float | None = None
    report: str | None = None
    error: str | None = None

    # Event log — appended to by the ACP session callback. Copied out via
    # snapshot() for /events SSE.
    events: list[Event] = field(default_factory=list)
    # Loop guard for the whole flow (guard.py); _guard_stop is set on a stop signal.
    depth: str = "deep"   # deep = lead + sub-agent team, quick = one agent (guard.Limits.quick)
    created_at: datetime | None = None
    parent_job: str | None = None
    guard: JobGuard = field(default_factory=JobGuard)
    _guard_stop: asyncio.Event = field(default_factory=asyncio.Event)
    _cond: asyncio.Condition | None = None
    _process: Any = None  # asyncio.subprocess.Process | None
    _proc_exit: Any = None  # task: proc.wait(), done when hermes is gone
    _seq: int = 0           # last event seq of this job
    _terminal_claimed: bool = False  # the first terminal path wins, the rest stay silent
    _tail: Any = None       # SubagentTail while the lead session lives


class Orchestrator:
    """Spawns + watches `hermes acp` sessions per research job."""

    def __init__(
        self,
        *,
        hermes_bin: str = "hermes",
        skills: list[str],
        jobs_dir: Path,
        env: dict[str, str],
        adapter_url_for_hermes: str = "http://localhost:8000",
        timeout_sec: int = 600,
        hermes_home: str | None = None,
        max_concurrent: int = 12,
        acp_init_timeout: float = 60,
        state_dir: Path | None = None,
    ) -> None:
        """
        hermes_bin: path to `hermes` executable (must be in $PATH of this process).
        jobs_dir: filesystem directory where each job gets its own workspace.
        env: LLM credentials / base URLs to pass through to Hermes.
        adapter_url_for_hermes: HTTP URL of *this* adapter, as reachable from
            the spawned hermes process. Since both run in the same container
            now, "http://localhost:8000" is the sane default.
        hermes_home: HERMES_HOME env var passed to subprocess (where skills/
            config.yaml live). Defaults to $HERMES_HOME or /opt/data.
        """
        self._hermes_bin = hermes_bin
        self._skills = skills
        self._jobs_dir = jobs_dir
        self._env = env
        self._adapter_url = adapter_url_for_hermes
        self._timeout = timeout_sec
        self._hermes_home = hermes_home or os.environ.get("HERMES_HOME", "/opt/data")
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        # Job queue: at most max_concurrent hermes processes at once, the rest wait
        # as "queued". One process takes ~200 MB at start and the gateway key has a
        # parallel cap; past either limit jobs die (OOM) or turn into 429s.
        self._slots = asyncio.Semaphore(max(1, max_concurrent))
        self._acp_init_timeout = acp_init_timeout
        # Adapter-owned state per job: job.json and events.jsonl. The agent's
        # workspace (jobs_dir/<id>) holds only what the agent itself writes.
        self._state_dir = Path(state_dir) if state_dir else self._jobs_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self.recover_interrupted()

    # ---------- public API ----------

    async def spawn(self, query: str, depth: str = "deep") -> str:
        job_id = uuid.uuid4().hex[:16]
        workspace = self._jobs_dir / job_id
        workspace.mkdir(parents=True, exist_ok=True)

        job = Job(
            id=job_id,
            query=query,
            status=JobStatus.queued,
            workspace_path=workspace,
            started_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            depth=depth,
        )
        if depth == "quick":
            job.guard = JobGuard(limits=Limits.quick())
        job._cond = asyncio.Condition()
        async with self._lock:
            self._jobs[job_id] = job
        self._write_meta(job)

        asyncio.create_task(self._run_queued(job_id, query))
        return job_id

    async def _run_queued(self, job_id: str, query: str) -> None:
        async with self._slots:
            job = self._jobs[job_id]
            if job.status != JobStatus.queued:  # cancelled while waiting
                return
            job.started_at = datetime.now(timezone.utc)  # wall time counts from the slot
            job.status = JobStatus.running
            self._write_meta(job)
            await self._run(job_id, query)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status not in (JobStatus.queued, JobStatus.running) or not _claim_terminal(job):
            return False
        job.finished_at = datetime.now(timezone.utc)
        if job.started_at:
            job.duration_sec = (job.finished_at - job.started_at).total_seconds()
        if job._process and job._process.returncode is None:
            try:
                job._process.terminate()
                try:
                    await asyncio.wait_for(job._process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    job._process.kill()
            except Exception:
                logger.exception("Failed to terminate hermes subprocess for %s", job_id)
        await self._drain_tail(job, final=True)
        await self._emit(job, Event.now(
            job_id=job_id, agent_id="lead", type="done",
            payload=self._final_payload(job, "cancelled"),
        ))
        job.status = JobStatus.cancelled
        await self._notify(job)
        return True

    def snapshot(self, job_id: str) -> list[Event]:
        job = self._jobs.get(job_id)
        if job is None:
            return []
        return list(job.events)

    async def subscribe(self, job_id: str, after: int = 0):
        """Async generator: yields new events for `job_id` as they arrive.

        Starts by replaying the full history, then blocks on the condition
        variable waiting for appends. Exits when the job reaches a terminal
        state AND all events up to that point have been yielded.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        idx = max(0, int(after or 0))  # seq == index + 1
        terminal = {
            JobStatus.completed, JobStatus.failed, JobStatus.timeout,
            JobStatus.cancelled, JobStatus.interrupted,
        }
        while True:
            # Snapshot under lock-free copy; events list only grows.
            current = job.events
            while idx < len(current):
                yield current[idx]
                idx += 1

            if job.status in terminal and idx >= len(job.events):
                return

            if job._cond is None:
                await asyncio.sleep(0.2)
                continue

            async with job._cond:
                # Wait up to 1s for a notify; periodic wake lets us re-check
                # status in case the writer crashed before notifying.
                try:
                    await asyncio.wait_for(job._cond.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    def read_logs(self, job_id: str) -> str | None:
        """Back-compat: return a plain-text dump of events (one per line).

        Old clients poll /logs and render with a regex parser; keep this
        working during the rollover. New clients should use /events SSE.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if not job.events:
            return None
        out: list[str] = []
        for e in job.events:
            out.append(f"[{e.ts}] {e.agent_id} {e.type}: {json.dumps(e.payload, ensure_ascii=False)[:400]}")
        return "\n".join(out)

    # ---------- internals ----------

    async def _emit(self, job: Job, ev: Event) -> None:
        job._seq += 1
        ev.seq = job._seq
        job.events.append(ev)
        # Persist to the adapter's state dir (not the agent-writable workspace).
        try:
            d = self._state_dir / job.id
            d.mkdir(parents=True, exist_ok=True)
            with (d / EVENTS_FILENAME).open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("failed to persist event", exc_info=True)
        cond = job._cond
        if cond is not None:
            async with cond:
                cond.notify_all()

    async def _run(self, job_id: str, query: str) -> None:
        job = self._jobs[job_id]
        await self._emit(job, Event.now(
            job_id=job_id, agent_id="lead", type="spawn",
            payload={"query": query, "skills": self._skills,
                     "hermes_bin": self._hermes_bin, "depth": job.depth,
                     "parent_job": job.parent_job, "limits": asdict(job.guard.limits)},
        ))

        # Lazy import — acp SDK lives inside the hermes venv.
        try:
            from acp import (
                PROTOCOL_VERSION, Client, RequestError,
                connect_to_agent, text_block,
            )
            from acp.schema import ClientCapabilities, Implementation
        except Exception as e:
            await self._fail(job, f"acp SDK import failed: {e}")
            return

        proc_env = {
            **os.environ,
            **self._env,
            "SEARCHARVESTER_URL": self._adapter_url,
            "HERMES_HOME": self._hermes_home,
            # One research = one finite session. Since Hermes v0.21 an interactive
            # (ACP) session runs delegate_task in the BACKGROUND: the lead says
            # "round 1 dispatched", ends its turn, prompt() returns and we would kill
            # the children. The one-shot marker makes delegation join its children
            # inside the tool call, hides skill_manage and trims skill coaching from
            # the prompt (less overhead per turn). Its per-session child cap is
            # delegation.oneshot_max_children in hermes-data/config.yaml.
            "HERMES_SINGLE_QUERY_SESSION": "1",
            # The image sets HERMES_WRITE_SAFE_ROOT=/opt/data, which denies every
            # write into the job workspace (report.md, plan.md, extracts). Narrow it
            # to this job's own directory: agents may write only there.
            "HERMES_WRITE_SAFE_ROOT": str(job.workspace_path),
            # The skill scripts tag /search and /extract with it, so the loop guard
            # counts and dedupes calls of all agents of this job together.
            "SEARCHARVESTER_JOB_ID": job.id,
        }

        # Subprocess
        try:
            proc = await asyncio.create_subprocess_exec(
                self._hermes_bin, "acp",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(job.workspace_path),
                env=proc_env,
                # Own session and process group per job. Hermes cleans up with
                # killpg and agents run shell commands; in the adapter's group
                # either could take uvicorn down (seen at 30 parallel jobs: PID 1
                # exited 0 and every running job was lost).
                start_new_session=True,
                # One ACP message is one JSON line on stdout. asyncio's default 64 KB
                # line limit killed a job on its last step: the write_file update for
                # a long report.md did not fit ("Separator is found, but chunk is
                # longer than limit").
                limit=ACP_LINE_LIMIT,
            )
        except FileNotFoundError:
            await self._fail(job, f"`{self._hermes_bin}` not found in PATH")
            return
        except Exception as e:
            await self._fail(job, f"failed to spawn hermes acp: {e}")
            return

        job._process = proc
        job.status = JobStatus.running

        # Drain stderr to hermes.log so we can debug any crashes
        stderr_task = asyncio.create_task(self._drain_stderr(job, proc))

        # Build ACP client wiring up our normalizer
        orch = self

        class _Forwarder(Client):
            async def session_update(
                self,
                session_id: str,
                update: Any,
                **_: Any,
            ) -> None:
                evs = normalize_acp_update(
                    update, job_id=job_id, agent_id="lead", parent_id=None,
                )
                for ev in evs:
                    tail = job._tail
                    if tail is not None and ev.agent_id.startswith("sub-"):
                        if ev.type == "spawn":
                            tail.register_spawn(ev.agent_id, ev.payload.get("goal", ""),
                                                ev.payload.get("delegate_call_id", ""),
                                                ev.payload.get("task_index", 0))
                        else:
                            if not tail.known(ev.agent_id):
                                for a_id, t_, pl in tail.adopt(ev.agent_id, ev.payload.get("delegate_call_id", ""),
                                                               _task_index(ev.agent_id)):
                                    await orch._emit(job, Event.now(job_id=job_id, agent_id=a_id,
                                                                    parent_id="lead", type=t_, payload=pl))
                            if ev.type == "done" and not tail.take_synthetic_done(ev.agent_id):
                                continue  # its state.db session closes it, one done per task
                    await orch._emit(job, ev)
                    job.guard.touch()
                    if ev.type == "message" and isinstance(ev.payload.get("text"), str):
                        await orch._guard_signals(job, job.guard.on_message(ev.payload["text"]))

            async def request_permission(self, options=None, session_id=None, tool_call=None, **k):
                # Hermes v0.21 asks before every file edit. Allow edits inside the
                # job workspace only; deny the rest (permissions.py).
                from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse
                allow, reason = permissions.decide(job.workspace_path, tool_call)
                option_id = permissions.pick_option(options, allow)
                await orch._emit(job, Event.now(
                    job_id=job_id, agent_id="lead", type="note",
                    payload={"kind": "permission", "allowed": allow and bool(option_id), "reason": reason},
                ))
                if allow and option_id:
                    return RequestPermissionResponse(outcome=AllowedOutcome(option_id=option_id, outcome="selected"))
                return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
            async def write_text_file(self, *a, **k):
                raise RequestError.method_not_found("fs/write_text_file")
            async def read_text_file(self, *a, **k):
                raise RequestError.method_not_found("fs/read_text_file")
            async def create_terminal(self, *a, **k):
                raise RequestError.method_not_found("terminal/create")
            async def terminal_output(self, *a, **k):
                raise RequestError.method_not_found("terminal/output")
            async def release_terminal(self, *a, **k):
                raise RequestError.method_not_found("terminal/release")
            async def wait_for_terminal_exit(self, *a, **k):
                raise RequestError.method_not_found("terminal/wait_for_exit")
            async def kill_terminal(self, *a, **k):
                raise RequestError.method_not_found("terminal/kill")
            async def ext_method(self, method, params):
                raise RequestError.method_not_found(method)
            async def ext_notification(self, method, params):
                raise RequestError.method_not_found(method)

        client = _Forwarder()
        conn = connect_to_agent(client, proc.stdin, proc.stdout)
        proc_exit = asyncio.create_task(proc.wait())
        job._proc_exit = proc_exit

        try:
            await _race_proc(proc_exit, conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(
                    name="searcharvester",
                    title="Searcharvester Orchestrator",
                    version="2.2.0",
                ),
            ), timeout=self._acp_init_timeout)
            session = await _race_proc(
                proc_exit, conn.new_session(mcp_servers=[], cwd=str(job.workspace_path)), timeout=self._acp_init_timeout)
            job._tail = SubagentTail(Path(self._hermes_home) / "state.db", session.session_id)

            # Preload skills via slash-command prompt prefix — `hermes acp` honours
            # the same `--skills` contract through the /skills slash command.
            # Simpler: shove skills load into the query text itself (agent reads
            # SKILL.md when it sees the name). That matches chat-mode behaviour.
            quick = job.depth == "quick"
            skills_hint = ", ".join(QUICK_SKILLS if quick else self._skills)
            # Build suffix per-call so the current-date hint stays fresh
            # even on long-running containers.
            wrapped = (
                f"Use these skills: {skills_hint}.\n\n"
                f"{query}"
                f"{_quick_suffix() if quick else _mandatory_suffix()}"
            )

            prompt_task = asyncio.create_task(
                conn.prompt(
                    session_id=session.session_id,
                    prompt=[text_block(wrapped)],
                )
            )

            # Sub-agents do not stream over ACP: tail their state.db sessions.
            watcher_task = asyncio.create_task(self._watch_subagents(job))

            idle_task = asyncio.create_task(self._watch_idle(job))
            try:
                stopped = await self._await_prompt_or_guard(job, prompt_task)
                if stopped:
                    await self._wrap_up_after_guard(job, conn, session.session_id, prompt_task, text_block)
            except asyncio.TimeoutError:
                prompt_task.cancel()
                if not _claim_terminal(job):
                    return
                job.error = f"exceeded timeout of {self._timeout}s"
                await self._drain_tail(job, final=True)
                await self._emit(job, Event.now(
                    job_id=job_id, agent_id="lead", type="done",
                    payload=self._final_payload(job, "timeout", error=job.error),
                ))
                job.status = JobStatus.timeout
                await self._notify(job)
                return
            finally:
                # Watcher cancellation is in finally so it runs on both
                # success and timeout paths. Double-cancel after return is
                # harmless.
                idle_task.cancel()
                watcher_task.cancel()
                try:
                    await watcher_task
                except (asyncio.CancelledError, Exception):
                    pass

            await self._finalize_success(job)

        except Exception as e:
            logger.exception("ACP session crashed for %s", job_id)
            await self._fail(job, f"ACP session error: {e}")
        finally:
            # Tidy subprocess if still alive.
            # Tidy the whole job group: hermes and whatever its agents left running.
            _signal_group(proc, signal.SIGTERM)
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    _signal_group(proc, signal.SIGKILL)
                except Exception:
                    pass
            _signal_group(proc, signal.SIGKILL)
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            job.finished_at = datetime.now(timezone.utc)
            if job.started_at:
                job.duration_sec = (job.finished_at - job.started_at).total_seconds()

    async def _watch_subagents(self, job: Job) -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                await self._drain_tail(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("sub-agent tail tick failed", exc_info=True)

    async def _drain_tail(self, job: Job, *, final: bool = False) -> None:
        """Emit new sub-agent events; final=True also closes every open task
        (called right before the lead's done on every terminal path)."""
        tail = job._tail
        if tail is None:
            return
        if final:
            job._tail = None
        triples = await asyncio.to_thread(tail.finish if final else tail.poll)
        for agent_id, type_, payload in triples:
            if type_ == "usage" and agent_id == "lead":
                continue
            await self._emit(job, Event.now(job_id=job.id, agent_id=agent_id, parent_id="lead",
                                            type=type_, payload=payload))
            job.guard.touch()

    def _final_payload(self, job: Job, status: str, **extra: Any) -> dict[str, Any]:
        """Counters and limits in every terminal done (A3), whatever the path."""
        payload = {"status": status, "guard": job.guard.stats(), "limits": asdict(job.guard.limits), **extra}
        if job.guard.tripped:
            payload["stopped_by_guard"] = job.guard.tripped.reason
        return payload

    # ---------- job metadata on disk (A4) ----------

    def _write_meta(self, job: Job) -> None:
        d = self._state_dir / job.id
        try:
            d.mkdir(parents=True, exist_ok=True)
            meta = {
                "id": job.id, "query": job.query, "depth": job.depth, "parent_job": job.parent_job,
                "created_at": job.created_at.isoformat() if job.created_at else None,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                "duration_sec": job.duration_sec, "status": job.status.value, "error": job.error,
            }
            if job.report is not None:
                (d / "final_report.md").write_text(job.report, encoding="utf-8")
                meta["report_file"] = "final_report.md"
            tmp = d / (JOB_META_FILENAME + ".tmp")
            tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, d / JOB_META_FILENAME)
        except Exception:
            logger.warning("failed to write job.json for %s", job.id, exc_info=True)

    def _read_events_file(self, job_id: str) -> list[dict[str, Any]]:
        """Events from disk; lines without seq (older files) get their line number."""
        out: list[dict[str, Any]] = []
        for base in (self._state_dir, self._jobs_dir):
            p = base / job_id / EVENTS_FILENAME
            if not p.exists():
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                d.setdefault("seq", n)
                if not d.get("seq"):
                    d["seq"] = n
                out.append(d)
            if out:
                break
        return out

    def load_meta(self, job_id: str) -> dict[str, Any] | None:
        """job.json, or a reconstruction from an older events.jsonl (lead only)."""
        p = self._state_dir / job_id / JOB_META_FILENAME
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                pass
        events = self._read_events_file(job_id)
        lead_spawn = next((e for e in events if e.get("type") == "spawn" and e.get("agent_id") == "lead"), None)
        if lead_spawn is None:
            return None
        lead_done = [e for e in events if e.get("type") == "done" and e.get("agent_id") == "lead"]
        status = (lead_done[-1].get("payload") or {}).get("status") if lead_done else "interrupted"
        return {"id": job_id, "query": (lead_spawn.get("payload") or {}).get("query", ""),
                "depth": (lead_spawn.get("payload") or {}).get("depth", "unknown"),
                "created_at": lead_spawn.get("ts"), "started_at": lead_spawn.get("ts"),
                "finished_at": lead_done[-1].get("ts") if lead_done else None,
                "status": status if status in TERMINAL else "interrupted", "legacy": True}

    def load_report(self, job_id: str, meta: dict[str, Any] | None = None) -> str | None:
        meta = meta or self.load_meta(job_id) or {}
        for p in ((self._state_dir / job_id / meta["report_file"]) if meta.get("report_file") else None,
                  self._jobs_dir / job_id / REPORT_FILENAME):
            if p is not None and p.exists():
                return p.read_text(encoding="utf-8", errors="replace")
        return None

    def disk_events(self, job_id: str) -> list[dict[str, Any]]:
        return self._read_events_file(job_id)

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first: live jobs from memory, finished ones from disk."""
        seen: dict[str, dict[str, Any]] = {}
        for base in (self._state_dir, self._jobs_dir):
            if not base.exists():
                continue
            for d in base.iterdir():
                if d.name in seen or not re.fullmatch(r"[0-9a-f]{16}", d.name):
                    continue
                meta = self.load_meta(d.name)
                if meta:
                    seen[d.name] = meta
        for job in self._jobs.values():
            seen[job.id] = {"id": job.id, "query": job.query, "depth": job.depth,
                            "created_at": job.created_at.isoformat() if job.created_at else None,
                            "started_at": job.started_at.isoformat() if job.started_at else None,
                            "duration_sec": job.duration_sec, "status": job.status.value,
                            "parent_job": job.parent_job}
        rows = sorted(seen.values(), key=lambda m: m.get("created_at") or "", reverse=True)
        return rows[:limit]

    def recover_interrupted(self) -> None:
        """At start: jobs left queued/running by a previous adapter are interrupted."""
        if not self._state_dir.exists():
            return
        for d in self._state_dir.iterdir():
            p = d / JOB_META_FILENAME
            if not p.exists():
                continue
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if meta.get("status") in ("queued", "running"):
                meta["status"] = "interrupted"
                meta["error"] = "adapter restarted while the job was " + str(meta.get("status"))
                n = len(self._read_events_file(d.name))
                ev = Event.now(job_id=d.name, agent_id="lead", type="done",
                               payload={"status": "interrupted"})
                ev.seq = n + 1
                with (d / EVENTS_FILENAME).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
                tmp = d / (JOB_META_FILENAME + ".tmp")
                tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, p)

    async def _drain_stderr(self, job: Job, proc: Any) -> None:
        """Append hermes stderr to hermes.log for debug."""
        if proc.stderr is None:
            return
        if job.workspace_path is None:
            return
        log_path = job.workspace_path / LOG_FILENAME
        tail = b""
        try:
            with log_path.open("ab") as f:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    f.write(chunk)
                    f.flush()
                    # Every agent of the job logs its LLM calls and failed tools
                    # here: the loop guard's view of the sub-agents.
                    *lines, tail = (tail + chunk).split(b"\n")
                    for line in lines:
                        await self._guard_signals(job, job.guard.on_log_line(line.decode("utf-8", "replace")))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("stderr drain error", exc_info=True)

    # ---------- loop guard ----------

    async def _guard_signals(self, job: Job, signals: list[Signal]) -> None:
        """Emit guard notes (queued warnings first) and raise the stop flag on a stop."""
        for sig in job.guard.pending_warnings() + list(signals):
            if sig.level == "stop" and job._guard_stop.is_set():
                continue  # one stop per job is enough; later ones only repeat it
            await self._emit(job, Event.now(
                job_id=job.id, agent_id="lead", type="note",
                payload={**sig.to_payload(), **job.guard.stats()},
            ))
            if sig.level == "stop":
                logger.warning("loop guard stops job %s: %s", job.id, sig.reason)
                job._guard_stop.set()

    async def _watch_idle(self, job: Job) -> None:
        while not job._guard_stop.is_set():
            await asyncio.sleep(10)
            sig = job.guard.check_idle()
            if sig:
                await self._guard_signals(job, [sig])

    async def _await_prompt_or_guard(self, job: Job, prompt_task: asyncio.Task) -> bool:
        """Wait for the lead's turn; True when the loop guard stopped it first.
        Raises asyncio.TimeoutError past the job timeout, like wait_for did."""
        stop_wait = asyncio.create_task(job._guard_stop.wait())
        waits = {prompt_task, stop_wait}
        if job._proc_exit is not None:
            waits.add(job._proc_exit)
        try:
            done, _ = await asyncio.wait(waits, timeout=self._timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_wait.cancel()
        if prompt_task in done:
            prompt_task.result()
            return False
        if job._proc_exit is not None and job._proc_exit in done:
            prompt_task.cancel()
            raise HermesExited(job._proc_exit.result())
        if not done:
            raise asyncio.TimeoutError
        return True

    async def _wrap_up_after_guard(self, job: Job, conn: Any, session_id: str,
                                   prompt_task: asyncio.Task, text_block: Any) -> None:
        """Cancel the looping turn, then give the lead one short turn to write
        report.md from what it has; search and extract are closed meanwhile."""
        try:
            await conn.cancel(session_id=session_id)
        except Exception:
            logger.debug("ACP cancel failed", exc_info=True)
        try:
            await asyncio.wait_for(asyncio.shield(prompt_task), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            prompt_task.cancel()
        report_path = (job.workspace_path or Path()) / REPORT_FILENAME
        if report_path.exists():
            return
        job.guard.wrapup = True
        reason = job.guard.tripped.reason if job.guard.tripped else "loop guard"
        await self._emit(job, Event.now(
            job_id=job.id, agent_id="lead", type="note",
            payload={"kind": "guard", "action": "wrapup", "reason": reason, **job.guard.stats()},
        ))
        wrap = (
            f"STOP. The research was stopped by the loop guard ({reason}). Do not search, "
            "read pages or delegate any more. Write report.md now in the workspace from the "
            "sources and notes you already have, cite only URLs you actually read, and mark "
            "what is left unverified. Then finish."
        )
        try:
            await asyncio.wait_for(
                conn.prompt(session_id=session_id, prompt=[text_block(wrap)]),
                timeout=job.guard.limits.wrapup_s,
            )
        except Exception:
            logger.info("wrap-up turn for %s ended without a clean finish", job.id, exc_info=True)

    async def _finalize_success(self, job: Job) -> None:
        """Emit the final `done` event BEFORE flipping job.status to terminal,
        otherwise the SSE subscriber can wake between the status flip and the
        event append, see (terminal + idx >= len) and return early — dropping
        the last event before the client sees it.
        """
        if not _claim_terminal(job):
            return
        await self._drain_tail(job, final=True)
        report_path = (job.workspace_path or Path()) / REPORT_FILENAME
        if report_path.exists():
            job.report = report_path.read_text(encoding="utf-8", errors="replace")
            payload = self._final_payload(job, "completed", report_bytes=len(job.report))
            await self._emit(job, Event.now(job_id=job.id, agent_id="lead", type="done", payload=payload))
            job.status = JobStatus.completed
            await self._notify(job)
            return

        msg_chunks = [
            e.payload.get("text", "") for e in job.events
            if e.type == "message" and isinstance(e.payload.get("text"), str)
        ]
        fallback = "".join(msg_chunks).strip()
        # A chat reply is a report only when it is one: long enough and sourced.
        # "Round 1 dispatched" or a refusal must not come out as a completed job.
        if len(fallback) >= 800 and _count_urls(fallback) >= 2 and not job.guard.tripped:
            job.report = fallback
            job.error = "no report.md — using assistant message"
            await self._emit(job, Event.now(
                job_id=job.id, agent_id="lead", type="done",
                payload=self._final_payload(job, "completed", note=job.error),
            ))
            job.status = JobStatus.completed
            await self._notify(job)
            return

        job.report = fallback or None
        job.error = (f"stopped by loop guard ({job.guard.tripped.reason}), no report.md"
                     if job.guard.tripped else "agent finished without report.md")
        await self._emit(job, Event.now(
            job_id=job.id, agent_id="lead", type="done",
            payload=self._final_payload(job, "failed", error=job.error),
        ))
        job.status = JobStatus.failed
        await self._notify(job)

    async def _notify(self, job: Job) -> None:
        self._write_meta(job)
        await self._notify_waiters(job)

    async def _notify_waiters(self, job: Job) -> None:
        """Wake the SSE subscriber after a terminal state change — without
        this a subscriber blocked in cond.wait() would keep waiting up to 1s
        before re-checking job.status and exiting the stream."""
        if job._cond is None:
            return
        async with job._cond:
            job._cond.notify_all()

    async def _fail(self, job: Job, error: str) -> None:
        if not _claim_terminal(job):
            return  # already cancelled, timed out or finished: keep that outcome
        job.error = error
        job.finished_at = datetime.now(timezone.utc)
        if job.started_at:
            job.duration_sec = (job.finished_at - job.started_at).total_seconds()
        await self._drain_tail(job, final=True)
        await self._emit(job, Event.now(
            job_id=job.id, agent_id="lead", type="done",
            payload=self._final_payload(job, "failed", error=error),
        ))
        job.status = JobStatus.failed
        await self._notify(job)


def _task_index(agent_id: str) -> int:
    """sub-<hash>-<n> (events._sub_agent_id, n from 1) -> 0-based task index."""
    try:
        return int(agent_id.rsplit("-", 1)[1]) - 1
    except (IndexError, ValueError):
        return 0


def _claim_terminal(job: Any) -> bool:
    """One terminal outcome per job. Cancel, timeout, failure and success race
    (a cancel kills hermes, and the dying session then looks like a failure)."""
    if job._terminal_claimed:
        return False
    job._terminal_claimed = True
    return True


class HermesExited(RuntimeError):
    def __init__(self, code: Any):
        hint = " (killed, likely out of memory)" if code in (-9, 137) else ""
        super().__init__(f"hermes exited with code {code}{hint}")


async def _race_proc(proc_exit: asyncio.Task, coro: Any, *, timeout: float) -> Any:
    """Await an ACP call, but fail at once when hermes dies: a dead peer leaves
    the call pending forever (seen: OOM kill right after new_session)."""
    call = asyncio.ensure_future(coro)
    done, _ = await asyncio.wait({call, proc_exit}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    if call in done:
        return call.result()
    call.cancel()
    if proc_exit in done:
        raise HermesExited(proc_exit.result())
    raise asyncio.TimeoutError(f"ACP call took over {timeout}s")


def _signal_group(proc: Any, sig: int) -> None:
    """Signal the job's own process group (start_new_session=True makes pgid == pid).
    Never the adapter's group: if the pgid is ours, only the process itself."""
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        if pid != os.getpgid(0):
            os.killpg(pid, sig)   # works while any member lives, even after the leader exited
        elif proc.returncode is None:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _count_urls(text: str) -> int:
    """Count unique http(s) URLs in a sub-agent's output — a proxy for
    "did this agent actually cite sources it extracted?" Zero URLs means
    the agent wrote a narrative from memory, which is exactly what the
    deep-research skill forbids.
    """
    return len(_extract_unique_urls(text))


def _extract_unique_urls(text: str) -> set[str]:
    """Pull every distinct http(s) URL out of a markdown blob."""
    if not text:
        return set()
    import re
    return set(re.findall(r"https?://[^\s)\]\"'<>]+", text))


