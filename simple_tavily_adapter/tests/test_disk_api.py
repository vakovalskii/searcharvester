"""API over jobs that live only on disk (after a restart): list, status, snapshot, SSE resume."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import main
from orchestrator import Orchestrator

JID = "00000000000000b1"


@pytest.fixture
def api(tmp_path, monkeypatch):
    state = tmp_path / "state"
    d = state / JID
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({
        "id": JID, "query": "disk q", "depth": "quick", "status": "completed",
        "created_at": "2026-09-30T10:00:00+00:00", "report_file": "final_report.md"}))
    (d / "final_report.md").write_text("ANSWER: disk")
    evs = [{"ts": f"t{i}", "job_id": JID, "agent_id": "lead", "parent_id": None, "type": t,
            "payload": {"status": "completed"} if t == "done" else {}, "seq": i}
           for i, t in enumerate(["spawn", "message", "message", "done"], start=1)]
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in evs))
    orch = Orchestrator(hermes_bin="x", skills=[], jobs_dir=tmp_path / "jobs", env={}, state_dir=state)
    monkeypatch.setattr(main, "orchestrator", orch)
    return TestClient(main.app, base_url="http://localhost")


def sse(api, url, **kw):
    # sse-starlette keeps a module-global asyncio.Event bound to the first loop;
    # TestClient starts a new loop per request, so reset it before each stream.
    from sse_starlette.sse import AppStatus
    AppStatus.should_exit_event = None
    return api.get(url, **kw).text


def sse_ids(text):
    return [int(line[3:].strip()) for line in text.splitlines() if line.startswith("id:")]


def test_list_status_snapshot_from_disk(api):
    jobs = api.get("/research").json()["jobs"]
    assert [j["id"] for j in jobs] == [JID] and jobs[0]["depth"] == "quick"
    st = api.get(f"/research/{JID}").json()
    assert st["status"] == "completed" and st["report"] == "ANSWER: disk" and st["depth"] == "quick"
    snap = api.get(f"/research/{JID}/snapshot").json()
    assert [e["seq"] for e in snap["events"]] == [1, 2, 3, 4]


def test_sse_from_disk_resumes_after_seq_and_last_event_id(api):
    assert sse_ids(sse(api, f"/research/{JID}/events")) == [1, 2, 3, 4]
    assert sse_ids(sse(api, f"/research/{JID}/events?after=2")) == [3, 4]
    assert sse_ids(sse(api, f"/research/{JID}/events", headers={"Last-Event-ID": "3"})) == [4]
    assert '"status": "completed"' in sse(api, f"/research/{JID}/events?after=4")


def test_unknown_job_is_404(api):
    assert api.get("/research/00000000000000ff").status_code == 404
    assert api.get("/research/00000000000000ff/snapshot").status_code == 404
