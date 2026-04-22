"""Unit tests for orchestrator — pure logic with fake Docker client."""
from __future__ import annotations

import asyncio

import pytest

from orchestrator import Orchestrator, JobStatus, REPORT_MARKER
from tests.conftest import FakeContainer


# ---------- spawn() ----------

@pytest.mark.asyncio
async def test_spawn_returns_hex_job_id(fake_docker, orch_config):
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="anything")
    assert len(job_id) == 16
    assert all(c in "0123456789abcdef" for c in job_id)


@pytest.mark.asyncio
async def test_spawn_creates_workspace_dir(fake_docker, orch_config, tmp_jobs_dir):
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    assert (tmp_jobs_dir / job_id).is_dir()


@pytest.mark.asyncio
async def test_spawn_passes_query_and_skills_to_hermes_cli(fake_docker, orch_config):
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    await orch.spawn(query="what is RAG")
    kw = fake_docker.containers.last_run_kwargs
    cmd = kw["command"]
    # Query should contain the user's text + the mandatory output contract suffix.
    q_index = cmd.index("-q") + 1
    passed_query = cmd[q_index]
    assert passed_query.startswith("what is RAG")
    assert "REPORT_SAVED" in passed_query  # orchestrator's mandatory contract
    assert "-s" in cmd
    skills_arg = cmd[cmd.index("-s") + 1]
    assert "searcharvester-deep-research" in skills_arg
    assert "--yolo" in cmd


@pytest.mark.asyncio
async def test_spawn_mounts_workspace_volume(fake_docker, orch_config, tmp_jobs_dir):
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    volumes = fake_docker.containers.last_run_kwargs["volumes"]
    workspace_host = str(tmp_jobs_dir / job_id)
    assert workspace_host in volumes
    assert volumes[workspace_host]["bind"] == "/workspace"


@pytest.mark.asyncio
async def test_spawn_container_start_failure_marks_job_failed(fake_docker, orch_config):
    fake_docker.containers.run_raises = RuntimeError("boom")
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    job = orch.get(job_id)
    assert job.status == JobStatus.failed
    assert "boom" in job.error


# ---------- watch() / result handling ----------

@pytest.mark.asyncio
async def test_watch_parses_report_saved_marker_and_reads_report(
    fake_docker, orch_config, tmp_jobs_dir
):
    fake_docker.containers.prepare(
        FakeContainer(
            _logs=f"working...\n{REPORT_MARKER} /workspace/report.md\n".encode()
        )
    )
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    # Simulate Hermes writing the report file into the workspace.
    (tmp_jobs_dir / job_id / "report.md").write_text(
        "# Title\n\nBody with citation [1]\n\n[1] https://x\n"
    )
    await asyncio.sleep(0.1)
    job = orch.get(job_id)
    assert job.status == JobStatus.completed
    assert "Title" in job.report
    # Log file persisted on disk
    assert (tmp_jobs_dir / job_id / "hermes.log").exists()


@pytest.mark.asyncio
async def test_watch_without_marker_marks_failed(fake_docker, orch_config):
    fake_docker.containers.prepare(
        FakeContainer(_logs=b"some chatter without marker\n")
    )
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    await asyncio.sleep(0.1)
    job = orch.get(job_id)
    assert job.status == JobStatus.failed
    assert "marker" in job.error.lower()


@pytest.mark.asyncio
async def test_watch_marker_but_no_report_md_falls_back_to_stdout(
    fake_docker, orch_config, tmp_jobs_dir
):
    """Lenient: marker printed, file missing, but agent put content in stdout.
    Should complete using stdout content (with an advisory note)."""
    body = (
        "Syncing bundled skills into ~/.hermes/skills/ ...\n"
        "Done: 0 new, 0 updated, 72 unchanged. 72 total bundled.\n"
        "\n"
        "# Short answer\n"
        "The capital of France is Paris.\n"
        "\n"
        f"{REPORT_MARKER} /workspace/report.md\n"
        "session_id: 20260422_120000_abcdef\n"
    )
    fake_docker.containers.prepare(FakeContainer(_logs=body.encode()))
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    await asyncio.sleep(0.1)
    job = orch.get(job_id)
    assert job.status == JobStatus.completed
    assert "Paris" in job.report
    assert "report.md missing" in job.error


@pytest.mark.asyncio
async def test_watch_no_marker_but_agent_refusal_is_still_completed(
    fake_docker, orch_config, tmp_jobs_dir
):
    """Lenient: no marker at all, but agent produced a response (refusal).
    Should complete with stdout content (with an advisory note)."""
    fake_docker.containers.prepare(
        FakeContainer(
            _logs=(
                b"Syncing bundled skills into ~/.hermes/skills/ ...\n"
                b"Done: 0 new, 0 updated, 72 unchanged. 72 total bundled.\n"
                b"session_id: 20260422_120000_abcdef\n"
                b"I'm sorry, but I can't help with that request for privacy reasons.\n"
            )
        )
    )
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    await asyncio.sleep(0.1)
    job = orch.get(job_id)
    assert job.status == JobStatus.completed
    assert "sorry" in job.report.lower()
    assert "did not save" in job.error


@pytest.mark.asyncio
async def test_watch_no_marker_and_empty_stdout_still_fails(
    fake_docker, orch_config, tmp_jobs_dir
):
    """Sanity: no marker AND nothing meaningful in stdout → still failed."""
    fake_docker.containers.prepare(
        FakeContainer(
            _logs=(
                b"Syncing bundled skills into ~/.hermes/skills/ ...\n"
                b"Done: 0 new, 0 updated, 72 unchanged. 72 total bundled.\n"
            )
        )
    )
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    await asyncio.sleep(0.1)
    job = orch.get(job_id)
    assert job.status == JobStatus.failed
    assert "no substantive response" in job.error.lower()


@pytest.mark.asyncio
async def test_watch_timeout_kills_container(fake_docker, orch_config):
    orch_config["timeout_sec"] = 1  # short

    def _slow():
        import time
        time.sleep(3)

    container = FakeContainer()
    container._side_effect_on_wait = _slow
    fake_docker.containers.prepare(container)

    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    await asyncio.sleep(2)
    job = orch.get(job_id)
    assert job.status == JobStatus.timeout
    assert container._killed is True
    assert container._removed is True


@pytest.mark.asyncio
async def test_concurrent_spawns_have_independent_state(fake_docker, orch_config):
    fake_docker.containers.prepare(FakeContainer(_logs=b"no marker"))
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    ids = await asyncio.gather(*[orch.spawn(query=f"q{i}") for i in range(3)])
    assert len(set(ids)) == 3
    for i in ids:
        assert orch.get(i) is not None


# ---------- cancel() ----------

@pytest.mark.asyncio
async def test_cancel_kills_running_container(fake_docker, orch_config):
    def _block():
        import time
        time.sleep(5)

    container = FakeContainer()
    container._side_effect_on_wait = _block
    fake_docker.containers.prepare(container)

    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    await asyncio.sleep(0.05)
    ok = await orch.cancel(job_id)
    assert ok is True
    job = orch.get(job_id)
    assert job.status == JobStatus.cancelled
    assert container._killed is True


@pytest.mark.asyncio
async def test_cancel_already_completed_is_noop(
    fake_docker, orch_config, tmp_jobs_dir
):
    fake_docker.containers.prepare(
        FakeContainer(_logs=f"{REPORT_MARKER} ok\n".encode())
    )
    orch = Orchestrator(docker_client=fake_docker, **orch_config)
    job_id = await orch.spawn(query="x")
    (tmp_jobs_dir / job_id / "report.md").write_text("#")
    await asyncio.sleep(0.1)
    assert orch.get(job_id).status == JobStatus.completed
    ok = await orch.cancel(job_id)
    assert ok is False
    assert orch.get(job_id).status == JobStatus.completed
