"""
Research job orchestrator.

Spawns a short-lived Hermes agent container per /research task, watches it to
completion or timeout, reads the produced report.md, and cleans up.

Design notes:
- Containers are NOT run with --rm so we can always read logs (stdout/stderr)
  after exit. We remove them manually in the watcher's finally block.
- The container must print `REPORT_SAVED:` to stdout at the end as a
  machine-readable success signal. Just writing report.md is not enough —
  the skill methodology requires both.
- Workspace for each job is a bind-mount of jobs/{job_id}/ on the host. Hermes
  writes plan.md, notes.md, report.md, and we surface report.md via the API.
- All async work runs on the event loop. Docker SDK calls are synchronous and
  run in the default threadpool via asyncio.to_thread.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

REPORT_MARKER = "REPORT_SAVED:"
REPORT_FILENAME = "report.md"
LOG_FILENAME = "hermes.log"

# Appended to every user query before handing it to Hermes. Ensures the agent
# always produces a file+marker, and that it leans toward "search the web and
# cite sources" rather than reflexive refusals on legitimate research tasks.
MANDATORY_SUFFIX = """

---
IMPORTANT — Role + output contract (applies to every task):

ROLE: You are a web-research assistant. Use the searcharvester-search and
searcharvester-extract tools to find publicly available information on the
open web, then summarise it with citations to the sources you actually read.
Do NOT refuse tasks that are valid public-web research — if the information
(public profiles, work contacts, press mentions, company websites, etc.) is
findable via a normal web search, your job is to gather it and cite where
each fact came from. Only decline if the request is clearly for harassment,
doxxing of a private individual, or other illegal/harmful intent. If a task
is ambiguous, run searches first, then report what you did (or didn't) find.

OUTPUT:
1. Write your final answer to `/workspace/report.md` as markdown using the
   terminal tool (e.g. `cat > /workspace/report.md << 'EOF' ... EOF`).
2. As the VERY LAST line of your response, print exactly:
   REPORT_SAVED: /workspace/report.md

Both steps are non-negotiable."""


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
    container_id: str | None = None
    workspace_path: Path | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_sec: float | None = None
    report: str | None = None
    error: str | None = None


class DockerContainerProto(Protocol):
    id: str

    def logs(self, **kwargs: Any) -> bytes: ...
    def wait(self, **kwargs: Any) -> dict: ...
    def kill(self, **kwargs: Any) -> None: ...
    def remove(self, **kwargs: Any) -> None: ...


class DockerContainersAPIProto(Protocol):
    def run(self, image: str, **kwargs: Any) -> DockerContainerProto: ...
    def get(self, cid: str) -> DockerContainerProto: ...


class DockerClientProto(Protocol):
    containers: DockerContainersAPIProto


class Orchestrator:
    """Spawns + watches ephemeral Hermes research jobs."""

    def __init__(
        self,
        docker_client: DockerClientProto,
        hermes_image: str,
        skills: list[str],
        jobs_dir: Path,
        env: dict[str, str],
        hermes_data_path: Path | None = None,
        jobs_host_dir: Path | None = None,
        hermes_data_host_path: Path | None = None,
        adapter_url_for_hermes: str = "http://host.docker.internal:8000",
        timeout_sec: int = 600,
        max_turns: int = 30,
    ) -> None:
        """
        jobs_dir: path INSIDE this (adapter) container where we mkdir + read reports.
        jobs_host_dir: same path as seen by the HOST Docker daemon (for bind mounts
                       into the spawned Hermes container). Defaults to jobs_dir
                       (fine for tests where no docker-in-docker is involved).
        hermes_data_path / hermes_data_host_path: same split for Hermes skills volume.
        """
        self._client = docker_client
        self._image = hermes_image
        self._skills = skills
        self._jobs_dir = jobs_dir
        self._jobs_host_dir = jobs_host_dir or jobs_dir
        self._hermes_data_path = hermes_data_path
        self._hermes_data_host_path = hermes_data_host_path or hermes_data_path
        self._env = env
        self._adapter_url = adapter_url_for_hermes
        self._timeout = timeout_sec
        self._max_turns = max_turns
        self._jobs: dict[str, Job] = {}
        self._containers: dict[str, DockerContainerProto] = {}
        self._lock = asyncio.Lock()

    # ---------- public API ----------

    async def spawn(self, query: str) -> str:
        """Start a research job, return its id."""
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
        async with self._lock:
            self._jobs[job_id] = job

        # Docker daemon sees the workspace under host-side path, not the one the
        # adapter container sees.
        workspace_host = self._jobs_host_dir / job_id

        try:
            container = await asyncio.to_thread(
                self._client.containers.run,
                self._image,
                **self._run_kwargs(query, workspace_host),
            )
        except Exception as e:
            logger.exception("Failed to start Hermes container for %s", job_id)
            job.status = JobStatus.failed
            job.error = f"Container start failed: {e}"
            job.finished_at = datetime.now(timezone.utc)
            return job_id

        job.container_id = container.id
        job.status = JobStatus.running
        self._containers[job_id] = container
        asyncio.create_task(self._watch(job_id))
        return job_id

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        """Kill a running job. Returns False if job unknown or already done."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job.status not in (JobStatus.queued, JobStatus.running):
            return False
        container = self._containers.get(job_id)
        if container is not None:
            try:
                await asyncio.to_thread(container.kill)
            except Exception:
                logger.exception("Failed to kill container for %s", job_id)
        job.status = JobStatus.cancelled
        job.finished_at = datetime.now(timezone.utc)
        if job.started_at:
            job.duration_sec = (job.finished_at - job.started_at).total_seconds()
        return True

    def read_logs(self, job_id: str) -> str | None:
        """Return the captured hermes.log contents, or None if not available yet."""
        job = self._jobs.get(job_id)
        if job is None or job.workspace_path is None:
            return None
        log_path = job.workspace_path / LOG_FILENAME
        if not log_path.exists():
            return None
        return log_path.read_text(encoding="utf-8", errors="replace")

    # ---------- private ----------

    def _run_kwargs(self, query: str, workspace_host: Path) -> dict[str, Any]:
        """Build kwargs for docker.containers.run for a research job.

        `workspace_host` is the path as visible to the HOST Docker daemon,
        used for bind-mount into the spawned Hermes container.
        """
        volumes: dict[str, dict[str, str]] = {
            str(workspace_host): {"bind": "/workspace", "mode": "rw"},
        }
        if self._hermes_data_host_path is not None:
            volumes[str(self._hermes_data_host_path)] = {
                "bind": "/opt/data",
                "mode": "rw",
            }
        env = {
            **self._env,
            "SEARCHARVESTER_URL": self._adapter_url,
        }
        wrapped_query = query + MANDATORY_SUFFIX
        return {
            "command": [
                "chat", "-Q", "-q", wrapped_query,
                "-s", ",".join(self._skills),
                "-t", "terminal",
                "--yolo",
                "--max-turns", str(self._max_turns),
            ],
            "volumes": volumes,
            "environment": env,
            "detach": True,
            "remove": False,  # we remove manually after reading logs
            "extra_hosts": {"host.docker.internal": "host-gateway"},
        }

    async def _watch(self, job_id: str) -> None:
        """Wait for container exit, classify result, clean up.

        A side-car coroutine flushes container stdout/stderr to
        jobs/{id}/hermes.log every ~1.5s so the UI's debug pane and the
        /logs endpoint can show progress while the job is running.
        """
        job = self._jobs[job_id]
        container = self._containers.get(job_id)
        if container is None:
            job.status = JobStatus.failed
            job.error = "Container reference lost"
            return

        async def _tail_logs() -> None:
            """Periodically snapshot container logs into hermes.log.

            docker-py's container.logs() returns everything produced so far,
            so each call gives a fresh full snapshot. Cheap for our log sizes.
            """
            while True:
                try:
                    logs_bytes = await asyncio.to_thread(container.logs)
                    self._persist_log(job, logs_bytes)
                except Exception:
                    # Container may have been removed already; swallow and retry
                    # next tick until cancellation.
                    pass
                await asyncio.sleep(1.5)

        tailer = asyncio.create_task(_tail_logs())

        try:
            await asyncio.wait_for(
                asyncio.to_thread(container.wait),
                timeout=self._timeout,
            )
            # Final read (guaranteed to include the REPORT_SAVED marker line).
            logs_bytes = await asyncio.to_thread(container.logs)
            self._classify_success(job, logs_bytes)
        except asyncio.TimeoutError:
            job.status = JobStatus.timeout
            job.error = f"Exceeded timeout of {self._timeout}s"
            try:
                await asyncio.to_thread(container.kill)
            except Exception:
                logger.exception("Failed to kill timed-out container %s", job_id)
            try:
                logs_bytes = await asyncio.to_thread(container.logs)
                self._persist_log(job, logs_bytes)
            except Exception:
                pass
        except Exception as e:
            job.status = JobStatus.failed
            job.error = f"Watch error: {e}"
            logger.exception("Watch error for %s", job_id)
        finally:
            tailer.cancel()
            try:
                await tailer
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception:
                logger.debug("Container remove failed (maybe already gone)")
            job.finished_at = datetime.now(timezone.utc)
            if job.started_at:
                job.duration_sec = (job.finished_at - job.started_at).total_seconds()
            self._containers.pop(job_id, None)

    def _classify_success(self, job: Job, logs_bytes: bytes) -> None:
        """Decide completed/failed based on logs + report.md existence.

        Classification policy (lenient):
        - Happy path: marker present AND report.md on disk → `completed`.
        - Marker without file: treat stdout as the report → `completed` with
          an advisory `error` note.
        - No marker but agent produced a substantive response (e.g. refusal,
          direct answer for a trivial query): use the cleaned stdout as the
          report → `completed` with advisory note.
        - Nothing meaningful produced: `failed` with helpful message.
        """
        self._persist_log(job, logs_bytes)
        logs = logs_bytes.decode("utf-8", errors="replace")
        has_marker = REPORT_MARKER in logs
        report_path = (job.workspace_path or Path()) / REPORT_FILENAME
        report_exists = report_path.exists()

        if has_marker and report_exists:
            job.status = JobStatus.completed
            job.report = report_path.read_text(encoding="utf-8", errors="replace")
            return

        stdout_response = self._extract_agent_response(logs)

        if has_marker and not report_exists:
            if stdout_response:
                job.status = JobStatus.completed
                job.report = stdout_response
                job.error = f"{REPORT_MARKER} printed but report.md missing — using stdout"
            else:
                job.status = JobStatus.failed
                job.error = f"{REPORT_MARKER} printed but no report content"
            return

        # No marker: the agent skipped the mandatory contract. Accept if stdout
        # has a real response (refusal, short answer, etc.).
        if stdout_response and len(stdout_response) >= 30:
            job.status = JobStatus.completed
            job.report = stdout_response
            job.error = "agent did not save report.md — using stdout response"
        else:
            job.status = JobStatus.failed
            job.error = (
                f"no {REPORT_MARKER} marker and no substantive response in logs"
            )

    @staticmethod
    def _extract_agent_response(logs: str) -> str:
        """Strip Hermes boilerplate from stdout, keep only the agent's reply."""
        drop_patterns = (
            "Syncing bundled skills",
            "skills_sync.py",
            "Token cost", "Tokens:", "Cost:", "cost/tokens",
        )
        drop_prefixes = (
            "Done:",                 # "Done: 0 new, 0 updated, 72 unchanged ..."
            "session_id:",
            "Resume this session with",
            "Session:", "Duration:", "Messages:",
            "⚠",                    # various warnings Hermes emits
        )
        kept: list[str] = []
        for line in logs.splitlines():
            stripped = line.strip()
            if any(p in line for p in drop_patterns):
                continue
            if any(stripped.startswith(p) for p in drop_prefixes):
                continue
            kept.append(line)
        text = "\n".join(kept).strip()
        # Collapse >2 blank lines in a row
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")
        return text

    def _persist_log(self, job: Job, logs_bytes: bytes) -> None:
        """Write container stdout/stderr to jobs/{id}/hermes.log."""
        if job.workspace_path is None:
            return
        try:
            (job.workspace_path / LOG_FILENAME).write_bytes(logs_bytes)
        except Exception:
            logger.exception("Failed to persist log for %s", job.id)
