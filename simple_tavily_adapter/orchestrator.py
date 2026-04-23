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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from events import Event, normalize_acp_update

logger = logging.getLogger(__name__)

REPORT_FILENAME = "report.md"
LOG_FILENAME = "hermes.log"
EVENTS_FILENAME = "events.jsonl"

# Appended to every user query. Keeps the agent honest about where the final
# report lives and nudges it away from reflexive refusals on legitimate
# public-web research tasks.
MANDATORY_SUFFIX = """

---
IMPORTANT — Role + output contract (applies to every task, no exceptions):

ROLE: You are the LEAD researcher of a deep-research pipeline. For EVERY
query — even questions that look simple or factual — you MUST follow the
searcharvester-deep-research methodology:

1. Decompose the question into 3–5 concrete sub-questions.
2. Fire ONE delegate_task call with tasks=[...] containing all sub-questions
   in a single batch (so the sub-agents run in parallel). Each task gets
   toolsets=["terminal"] and the context template from the skill.
3. When delegate_task returns, synthesise the sub-agents' findings into a
   cited markdown report.

RULES:
- Do NOT run searcharvester-search or searcharvester-extract yourself.
  Those are for the sub-agents, not you.
- Do NOT answer from your own training knowledge. Every factual claim in
  the final report must come from a source a sub-agent extracted.
- Do NOT skip delegate_task — even for "who won X" style questions. If
  you catch yourself about to answer directly, stop and delegate instead.
- The searcharvester-deep-research skill is mandatory; load it via the
  skill tool at the start of every run.

OUTPUT: Your working directory is already the job workspace. Write the
final report as markdown to `./report.md` (relative path — do not use
/workspace/, that path does not exist). This file is what the user sees.
The report must have a TL;DR, findings with inline [n] citations, and a
References section listing every URL the sub-agents cited."""


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    timeout = "timeout"
    cancelled = "cancelled"


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
    _cond: asyncio.Condition | None = None
    _process: Any = None  # asyncio.subprocess.Process | None


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

    # ---------- public API ----------

    async def spawn(self, query: str) -> str:
        job_id = uuid.uuid4().hex[:16]
        workspace = self._jobs_dir / job_id
        workspace.mkdir(parents=True, exist_ok=True)

        job = Job(
            id=job_id,
            query=query,
            status=JobStatus.queued,
            workspace_path=workspace,
            started_at=datetime.now(timezone.utc),
        )
        job._cond = asyncio.Condition()
        async with self._lock:
            self._jobs[job_id] = job

        asyncio.create_task(self._run(job_id, query))
        return job_id

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status not in (JobStatus.queued, JobStatus.running):
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
        await self._emit(job, Event.now(
            job_id=job_id, agent_id="lead", type="done",
            payload={"status": "cancelled"},
        ))
        job.status = JobStatus.cancelled
        await self._notify(job)
        return True

    def snapshot(self, job_id: str) -> list[Event]:
        job = self._jobs.get(job_id)
        if job is None:
            return []
        return list(job.events)

    async def subscribe(self, job_id: str):
        """Async generator: yields new events for `job_id` as they arrive.

        Starts by replaying the full history, then blocks on the condition
        variable waiting for appends. Exits when the job reaches a terminal
        state AND all events up to that point have been yielded.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return
        idx = 0
        terminal = {
            JobStatus.completed, JobStatus.failed,
            JobStatus.timeout, JobStatus.cancelled,
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
        job.events.append(ev)
        # Persist to events.jsonl for post-mortem debugging.
        if job.workspace_path:
            try:
                with (job.workspace_path / EVENTS_FILENAME).open("a", encoding="utf-8") as f:
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
                     "hermes_bin": self._hermes_bin},
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
                    await orch._emit(job, ev)

            async def request_permission(self, *a, **k):
                raise RequestError.method_not_found("session/request_permission")
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

        try:
            await conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(
                    name="searcharvester",
                    title="Searcharvester Orchestrator",
                    version="2.2.0",
                ),
            )
            session = await conn.new_session(mcp_servers=[], cwd=str(job.workspace_path))

            # Preload skills via slash-command prompt prefix — `hermes acp` honours
            # the same `--skills` contract through the /skills slash command.
            # Simpler: shove skills load into the query text itself (agent reads
            # SKILL.md when it sees the name). That matches chat-mode behaviour.
            skills_hint = ", ".join(self._skills)
            wrapped = (
                f"Use these skills: {skills_hint}.\n\n"
                f"{query}"
                f"{MANDATORY_SUFFIX}"
            )

            prompt_task = asyncio.create_task(
                conn.prompt(
                    session_id=session.session_id,
                    prompt=[text_block(wrapped)],
                )
            )

            try:
                await asyncio.wait_for(prompt_task, timeout=self._timeout)
            except asyncio.TimeoutError:
                prompt_task.cancel()
                job.error = f"exceeded timeout of {self._timeout}s"
                await self._emit(job, Event.now(
                    job_id=job_id, agent_id="lead", type="done",
                    payload={"status": "timeout", "error": job.error},
                ))
                job.status = JobStatus.timeout
                await self._notify(job)
                return

            # Prompt returned. Before finalising, backfill any sub-agent
            # events that ACP truncated away (it caps tool_result content
            # around 2000 chars, so subs past index ~1 silently hang).
            await self._backfill_subagents(job, session.session_id)

            await self._finalize_success(job)

        except Exception as e:
            logger.exception("ACP session crashed for %s", job_id)
            await self._fail(job, f"ACP session error: {e}")
        finally:
            # Tidy subprocess if still alive.
            if proc.returncode is None:
                try:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        proc.kill()
                except Exception:
                    pass
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            job.finished_at = datetime.now(timezone.utc)
            if job.started_at:
                job.duration_sec = (job.finished_at - job.started_at).total_seconds()

    async def _backfill_subagents(self, job: Job, session_id: str) -> None:
        """Read the lead's Hermes session file on disk and emit `message` +
        `done` events for sub-agents that never got a terminal state through
        the ACP stream.

        The ACP adapter in Hermes truncates `tool_call/progress.content` to
        ~2000 chars, so for a delegate_task batch with 3+ children the tail
        results are never visible over the wire. The session file keeps the
        full un-truncated JSON, so we backfill from there post-prompt.
        """
        session_path = Path(self._hermes_home) / "sessions" / f"session_{session_id}.json"
        if not session_path.exists():
            logger.debug("no session file at %s — skipping backfill", session_path)
            return
        try:
            data = await asyncio.to_thread(
                lambda: json.loads(session_path.read_text(encoding="utf-8", errors="replace"))
            )
        except Exception:
            logger.exception("failed to read lead session file for backfill")
            return

        from events import _extract_delegate_results_from_text, _sub_agent_id

        messages = data.get("messages") or []

        # Pre-index: which sub_ids already have a done event from ACP?
        done_sub_ids: set[str] = {
            e.agent_id for e in job.events
            if e.type == "done" and e.parent_id == "lead"
        }
        message_sub_ids: set[str] = {
            e.agent_id for e in job.events
            if e.type == "message" and e.parent_id == "lead"
        }

        # Walk assistant → tool pairs looking for delegate_task calls.
        for i, msg in enumerate(messages):
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if not _is_delegate_function_name(fn.get("name")):
                    continue
                acp_call_id = _match_acp_delegate_call_id(
                    job_events=job.events,
                    sess_call_index=i,
                    session_messages=messages,
                )
                if acp_call_id is None:
                    # Fallback: walk delegate calls in job.events in order
                    # and match by position.
                    acp_call_id = _nth_delegate_call_id(job.events, _delegate_index(messages, i))
                if acp_call_id is None:
                    continue

                # Find the matching tool response in session messages
                content = _find_tool_response(messages, tc.get("id"), after=i)
                if not content:
                    continue
                results = _extract_delegate_results_from_text(content)
                if not results:
                    continue

                # Map sub-question goal → sub-agent session file, so we can
                # fall back on the sub's own session when Hermes wrote
                # "(empty)" or an otherwise useless summary.
                sess_dir = Path(self._hermes_home) / "sessions"
                sub_sessions_by_goal = _index_sub_sessions(sess_dir, session_id)

                for r in results:
                    idx = r.get("task_index")
                    if idx is None:
                        continue
                    sub_id = _sub_agent_id(acp_call_id, int(idx) + 1)
                    summary = (r.get("summary") or "").strip()

                    diagnostic: str | None = None
                    used_status = r.get("status", "completed")

                    if _is_useless_summary(summary):
                        recovered, diag = _recover_from_sub_session(
                            sub_sessions_by_goal, idx, messages, tc.get("id")
                        )
                        if recovered:
                            summary = recovered
                        if diag:
                            diagnostic = diag
                            # If the model didn't produce any content, the
                            # sub effectively failed — surface that.
                            if not recovered:
                                used_status = "failed"

                    if summary and sub_id not in message_sub_ids:
                        await self._emit(job, Event.now(
                            job_id=job.id, agent_id=sub_id, parent_id="lead",
                            type="message",
                            payload={"text": summary, "backfilled": True},
                        ))
                    if sub_id not in done_sub_ids:
                        payload: dict[str, Any] = {
                            "status": used_status,
                            "error": r.get("error") or diagnostic,
                            "delegate_call_id": acp_call_id,
                            "backfilled": True,
                        }
                        if diagnostic:
                            payload["note"] = diagnostic
                        await self._emit(job, Event.now(
                            job_id=job.id, agent_id=sub_id, parent_id="lead",
                            type="done", payload=payload,
                        ))

    async def _drain_stderr(self, job: Job, proc: Any) -> None:
        """Append hermes stderr to hermes.log for debug."""
        if proc.stderr is None:
            return
        if job.workspace_path is None:
            return
        log_path = job.workspace_path / LOG_FILENAME
        try:
            with log_path.open("ab") as f:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    f.write(chunk)
                    f.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("stderr drain error", exc_info=True)

    async def _finalize_success(self, job: Job) -> None:
        """Emit the final `done` event BEFORE flipping job.status to terminal,
        otherwise the SSE subscriber can wake between the status flip and the
        event append, see (terminal + idx >= len) and return early — dropping
        the last event before the client sees it.
        """
        report_path = (job.workspace_path or Path()) / REPORT_FILENAME
        if report_path.exists():
            job.report = report_path.read_text(encoding="utf-8", errors="replace")
            await self._emit(job, Event.now(
                job_id=job.id, agent_id="lead", type="done",
                payload={"status": "completed", "report_bytes": len(job.report)},
            ))
            job.status = JobStatus.completed
            await self._notify(job)
            return

        msg_chunks = [
            e.payload.get("text", "") for e in job.events
            if e.type == "message" and isinstance(e.payload.get("text"), str)
        ]
        fallback = "".join(msg_chunks).strip()
        if fallback:
            job.report = fallback
            job.error = "no report.md — using assistant message"
            await self._emit(job, Event.now(
                job_id=job.id, agent_id="lead", type="done",
                payload={"status": "completed", "note": job.error},
            ))
            job.status = JobStatus.completed
            await self._notify(job)
            return

        job.error = "agent finished without report.md or any message"
        await self._emit(job, Event.now(
            job_id=job.id, agent_id="lead", type="done",
            payload={"status": "failed", "error": job.error},
        ))
        job.status = JobStatus.failed
        await self._notify(job)

    async def _notify(self, job: Job) -> None:
        """Wake the SSE subscriber after a terminal state change — without
        this a subscriber blocked in cond.wait() would keep waiting up to 1s
        before re-checking job.status and exiting the stream."""
        if job._cond is None:
            return
        async with job._cond:
            job._cond.notify_all()

    async def _fail(self, job: Job, error: str) -> None:
        job.error = error
        job.finished_at = datetime.now(timezone.utc)
        if job.started_at:
            job.duration_sec = (job.finished_at - job.started_at).total_seconds()
        await self._emit(job, Event.now(
            job_id=job.id, agent_id="lead", type="done",
            payload={"status": "failed", "error": error},
        ))
        job.status = JobStatus.failed
        await self._notify(job)


def _is_delegate_function_name(name: Any) -> bool:
    if not name:
        return False
    n = str(name).lower()
    return "delegate" in n and ("task" in n or "tasks" in n)


def _delegate_index(messages: list[Any], i: int) -> int:
    """Count how many delegate_task assistant messages we've seen up to (and
    including) index i. Gives us a 0-based ordinal to align with job.events
    delegate tool_calls."""
    seen = -1
    for j, m in enumerate(messages[: i + 1]):
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if _is_delegate_function_name(fn.get("name")):
                seen += 1
    return seen


def _match_acp_delegate_call_id(
    *, job_events: list[Any], sess_call_index: int, session_messages: list[Any],
) -> str | None:
    """Best-effort: use ordinal position of this delegate call inside the
    session to pick the Nth delegate tool_call_id from the ACP event stream.
    Returns None when counts don't line up (we then fall back to fuzzy match
    by goal). Most jobs fire delegate_task once, so this usually matches on
    ordinal 0 directly.
    """
    ordinal = _delegate_index(session_messages, sess_call_index)
    return _nth_delegate_call_id(job_events, ordinal)


def _nth_delegate_call_id(job_events: list[Any], n: int) -> str | None:
    seen = -1
    for e in job_events:
        if e.type != "tool_call" or e.agent_id != "lead":
            continue
        title = str((e.payload or {}).get("title") or "")
        if not ("delegate" in title.lower() and "task" in title.lower()):
            continue
        seen += 1
        if seen == n:
            return (e.payload or {}).get("id")
    return None


def _find_tool_response(messages: list[Any], tc_id: Any, *, after: int) -> str:
    """Scan forward from `after` to find the `role: tool` message whose
    tool_call_id matches `tc_id`. Returns its content string (may be huge —
    that's the point)."""
    if not tc_id:
        return ""
    for m in messages[after + 1 :]:
        if m.get("role") != "tool":
            continue
        if m.get("tool_call_id") == tc_id:
            c = m.get("content", "")
            return c if isinstance(c, str) else str(c)
    return ""


_USELESS_SUMMARIES = {"", "(empty)", "none", "null", "n/a", "na"}


def _is_useless_summary(s: str) -> bool:
    """Detect Hermes' placeholder for a sub-agent that returned no content."""
    return s.strip().lower() in _USELESS_SUMMARIES or len(s.strip()) < 6


def _index_sub_sessions(
    sess_dir: Path, lead_session_id: str
) -> dict[str, dict[str, Any]]:
    """Map first-user-message prefix → sub-agent session data, scoped to the
    window around when the lead session was last updated.

    Sub-agent sessions are timestamp-named files like
    session_20260423_083028_550f93.json; the lead is UUID-named. We skip
    the lead file and anything whose first message doesn't look like a
    sub-question prompt."""
    out: dict[str, dict[str, Any]] = {}
    lead_path = sess_dir / f"session_{lead_session_id}.json"
    try:
        lead_mtime = lead_path.stat().st_mtime
    except Exception:
        return out
    import time
    window = 1800  # 30 min — ample for multi-batch deep research
    for p in sess_dir.glob("session_*.json"):
        if p == lead_path:
            continue
        try:
            mtime = p.stat().st_mtime
        except Exception:
            continue
        if abs(mtime - lead_mtime) > window:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        msgs = data.get("messages") or []
        if not msgs:
            continue
        first = msgs[0]
        if first.get("role") != "user":
            continue
        goal_text = str(first.get("content") or "").strip()
        if not goal_text:
            continue
        # Use a 60-char prefix as the key — robust to truncation on either end.
        key = goal_text[:60]
        out[key] = data
    return out


def _recover_from_sub_session(
    sub_sessions_by_goal: dict[str, dict[str, Any]],
    task_index: Any,
    lead_messages: list[Any],
    delegate_tc_id: Any,
) -> tuple[str, str | None]:
    """Try to fish out the sub-agent's real content when Hermes logged
    "(empty)". Returns (recovered_summary, diagnostic_note).

    - recovered_summary: empty if we couldn't find one
    - diagnostic_note: human-readable reason (e.g. "finish_reason=incomplete,
      no content generated") suitable for display in the UI
    """
    goal = _goal_for_task_index(lead_messages, delegate_tc_id, task_index)
    if not goal:
        return ("", "could not locate sub-agent goal in lead session")
    data = sub_sessions_by_goal.get(goal[:60])
    if data is None:
        return ("", f"no sub-agent session file matched goal prefix")

    msgs = data.get("messages") or []
    # Walk assistants backwards: prefer the last one that wrote real content.
    last_assistant_content: str = ""
    last_finish: str = ""
    reasoning_chunks: list[str] = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        c = m.get("content") or ""
        if isinstance(c, str) and c.strip():
            last_assistant_content = c
        last_finish = str(m.get("finish_reason") or last_finish)
        r = m.get("reasoning")
        if isinstance(r, str) and r.strip():
            reasoning_chunks.append(r)

    if last_assistant_content.strip():
        return (last_assistant_content, None)

    # No content; build a diagnostic.
    parts: list[str] = []
    if last_finish:
        parts.append(f"finish_reason={last_finish}")
    if reasoning_chunks:
        total_reasoning = sum(len(x) for x in reasoning_chunks)
        parts.append(f"{total_reasoning}b of reasoning, no content")
    else:
        parts.append("no reasoning either")
    return ("", "; ".join(parts) or "sub-agent produced no content")


def _goal_for_task_index(
    lead_messages: list[Any], delegate_tc_id: Any, task_index: Any
) -> str:
    """Pull sub-question N's goal text from the lead's `delegate_task` call
    arguments, keyed by task_index."""
    if delegate_tc_id is None:
        return ""
    for m in lead_messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if tc.get("id") != delegate_tc_id:
                continue
            fn = tc.get("function") or {}
            args_raw = fn.get("arguments")
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except Exception:
                    continue
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                continue
            tasks = args.get("tasks") if isinstance(args, dict) else None
            if not isinstance(tasks, list):
                return ""
            try:
                task = tasks[int(task_index)]
            except (IndexError, ValueError, TypeError):
                return ""
            if isinstance(task, dict):
                return str(task.get("goal") or "")
    return ""


