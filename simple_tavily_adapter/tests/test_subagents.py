"""subagents.py: live sub-agent steps from state.db, one identity and one done per task."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from subagents import SubagentTail

LEAD = "7a9f7705-6975-4636-ab0a-f1585dcde632"
SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL NOT NULL,
  ended_at REAL, end_reason TEXT, tool_call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT, tool_calls TEXT, tool_name TEXT, tool_call_id TEXT, timestamp REAL NOT NULL);
"""


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.c = sqlite3.connect(path)
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)

    def session(self, sid, goal, *, parent=LEAD, started=1.0):
        self.c.execute("INSERT INTO sessions (id, source, parent_session_id, started_at) VALUES (?,?,?,?)",
                       (sid, "subagent", parent, started))
        if goal is not None:
            self.msg(sid, "user", goal)
        else:
            self.c.commit()

    def msg(self, sid, role, content="", tool_calls=None, tool_call_id=None, tool_name=None):
        self.c.execute("INSERT INTO messages (session_id, role, content, tool_calls, tool_name, tool_call_id, timestamp)"
                       " VALUES (?,?,?,?,?,?,?)", (sid, role, content, tool_calls, tool_name, tool_call_id, 2.0))
        self.c.commit()

    def call(self, sid, cid, command):
        self.msg(sid, "assistant", "", json.dumps([{"id": cid, "type": "function", "function": {
            "name": "terminal", "arguments": json.dumps({"command": command})}}]))

    def end(self, sid, reason="agent_close", tokens=(100, 10, 3)):
        self.c.execute("UPDATE sessions SET ended_at=3.0, end_reason=?, input_tokens=?, output_tokens=?,"
                       " tool_call_count=? WHERE id=?", (reason, *tokens, sid))
        self.c.commit()


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "state.db")


def tail(db):
    t = SubagentTail(db.path, LEAD)
    t.register_spawn("sub-aa-1", "Researcher 1: SDD meaning", "call-A", 0)
    t.register_spawn("sub-aa-2", "Researcher 2: AI PDLC", "call-A", 1)
    return t


def of(events, type_=None, agent=None):
    return [e for e in events if (type_ is None or e[1] == type_) and (agent is None or e[0] == agent)]


def test_steps_come_under_the_task_identity(db):
    t = tail(db)
    db.session("s1", "Today's date is 2026-09-29.\nResearcher 1: SDD meaning\ncontext...")
    db.call("s1", "c1", "python3 search.py --query SDD")
    db.msg("s1", "tool", json.dumps({"output": "results", "exit_code": 0}), tool_call_id="c1", tool_name="terminal")
    ev = t.poll()
    assert [e[1] for e in ev if e[0] == "sub-aa-1"][:2] == ["tool_call", "tool_result"]
    call = of(ev, "tool_call")[0][2]
    assert call["title"].startswith("terminal: python3 search.py")
    assert of(ev, "tool_result")[0][2]["status"] == "completed"
    assert not of(ev, "spawn")  # the goal is already in the synthetic spawn


def test_cursor_neither_loses_nor_repeats(db):
    t = tail(db)
    db.session("s1", "Researcher 1: SDD meaning")
    db.call("s1", "c1", "a")
    first = t.poll()
    db.call("s1", "c2", "b")
    second = t.poll()
    third = t.poll()
    ids = [e[2]["id"] for e in first + second + third if e[1] == "tool_call"]
    assert ids == ["c1", "c2"]


def test_one_done_per_task_when_both_producers_speak(db):
    t = tail(db)
    db.session("s1", "Researcher 1: SDD meaning")
    t.poll()
    assert t.take_synthetic_done("sub-aa-1") is False  # covered: the database will close it
    db.end("s1")
    dones = of(t.poll() + t.finish(), "done", "sub-aa-1")
    assert len(dones) == 1 and dones[0][2]["status"] == "completed"


def test_uncovered_task_keeps_the_synthetic_done(db):
    t = tail(db)
    assert t.take_synthetic_done("sub-aa-2") is True
    assert t.take_synthetic_done("sub-aa-2") is False  # never twice
    assert not of(t.finish(), "done", "sub-aa-2")


def test_foreign_lead_is_ignored(db):
    t = tail(db)
    db.session("x1", "Researcher 1: SDD meaning", parent="another-lead")
    assert t.poll() == []


def test_late_match_is_glued_with_an_alias(db):
    t = SubagentTail(db.path, LEAD)
    db.session("s9abcdefgh", "Researcher 3: late one")
    ev = t.poll()
    assert of(ev, "spawn")[0][0] == "sub-db-abcdefgh"
    t.register_spawn("sub-bb-1", "Researcher 3: late one", "call-B", 0)
    ev = t.poll()
    alias = of(ev, "note")[0]
    assert alias[2] == {"kind": "alias", "from": "sub-db-abcdefgh", "to": "sub-bb-1"}
    db.call("s9abcdefgh", "c1", "x")
    assert of(t.poll(), "tool_call")[0][0] == "sub-bb-1"


def test_unmatched_session_stays_visible_and_is_closed(db):
    t = tail(db)
    db.session("s7zzzzzzzz", "something the lead never delegated")
    ev = t.poll()
    assert of(ev, "spawn")[0][2]["unmatched"] is True
    fin = t.finish()
    assert of(fin, "done", "sub-db-zzzzzzzz")[0][2]["status"] == "unknown"


def test_same_goal_twice_goes_by_batch_order(db):
    t = SubagentTail(db.path, LEAD)
    t.register_spawn("sub-cc-1", "Researcher: same", "call-C", 0)
    t.register_spawn("sub-cc-2", "Researcher: same", "call-C", 1)
    db.session("s1", "Researcher: same", started=1.0)
    db.session("s2", "Researcher: same", started=2.0)
    db.call("s1", "c1", "one")
    db.call("s2", "c2", "two")
    ev = of(t.poll(), "tool_call")
    assert {e[2]["id"]: e[0] for e in ev} == {"c1": "sub-cc-1", "c2": "sub-cc-2"}


def test_database_outage_then_recovery_gives_one_done_each(db, tmp_path):
    t = tail(db)
    db.session("s1", "Researcher 1: SDD meaning")
    t.poll()
    real = t.db_path
    t.db_path = tmp_path / "gone" / "state.db"  # the file vanished for a while
    assert t.poll() == []
    t.db_path = real
    db.end("s1")
    t.take_synthetic_done("sub-aa-2")  # uncovered task closed by its delegate result
    everything = t.poll() + t.finish()
    assert len(of(everything, "done", "sub-aa-1")) == 1
    assert of(everything, "done", "sub-aa-2") == []  # already closed synthetically


def test_truncated_batch_without_database_closes_the_rest_as_unknown(tmp_path):
    t = SubagentTail(tmp_path / "missing.db", LEAD)
    for i in range(3):
        t.register_spawn(f"sub-dd-{i + 1}", f"task {i}", "call-D", i)
    assert t.take_synthetic_done("sub-dd-1") and t.take_synthetic_done("sub-dd-2")  # only two in the result
    fin = t.finish()
    assert [(e[0], e[2]["status"]) for e in of(fin, "done")] == [("sub-dd-3", "unknown")]


def test_missing_column_switches_the_tail_off(tmp_path):
    p = tmp_path / "state.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    c.commit()
    t = SubagentTail(p, LEAD)
    assert t.poll() == [] and t.available is False
    t.register_spawn("sub-ee-1", "g", "call-E", 0)
    assert t.take_synthetic_done("sub-ee-1") is True


def test_wal_database_without_shm_is_readable(db):
    """mode=ro fails here; the tail must not."""
    t = tail(db)
    db.session("s1", "Researcher 1: SDD meaning")
    db.call("s1", "c1", "x")
    db.c.close()
    for suffix in ("-shm",):
        p = Path(str(db.path) + suffix)
        if p.exists():
            p.unlink()
    assert of(t.poll(), "tool_call")


def test_failed_tool_and_usage(db):
    t = tail(db)
    db.session("s1", "Researcher 1: SDD meaning")
    db.call("s1", "c1", "x")
    db.msg("s1", "tool", json.dumps({"output": "boom", "exit_code": 2}), tool_call_id="c1", tool_name="terminal")
    db.end("s1", tokens=(500, 50, 1))
    ev = t.poll()
    assert of(ev, "tool_result")[0][2]["status"] == "failed"
    assert of(ev, "usage")[-1][2] == {"input_tokens": 500, "output_tokens": 50, "tool_call_count": 1}


def test_goal_prefixes_from_the_acp_preview_match_sessions(db):
    """v0.21 sends raw_input=null for delegate_task: goals come from the preview."""
    from events import _tasks_from_preview, _tasks_from_title
    preview = ("Delegating 2 tasks\n\n1. Researcher: sub-question 1 — Архитектурные различия SGLang и\n"
               "... (179 chars total, truncated)\n2. Researcher: sub-question 2 — Сравнительные бенчмарки SGLang \n"
               "... (162 chars total, truncated)")
    tasks = _tasks_from_preview(preview)
    assert [t["goal"] for t in tasks] == ["Researcher: sub-question 1 — Архитектурные различия SGLang и",
                                         "Researcher: sub-question 2 — Сравнительные бенчмарки SGLang"]
    assert len(_tasks_from_title("delegate_task: 3 tasks: Researcher 1: SDD — w... | Researcher 2: AI...")) == 3
    t = SubagentTail(db.path, LEAD)
    for i, task in enumerate(tasks):
        t.register_spawn(f"sub-ff-{i + 1}", task["goal"], "call-F", i)
    db.session("s2", "Researcher: sub-question 2 — Сравнительные бенчмарки SGLang vs vLLM для MoE", started=1.0)
    db.session("s1", "Researcher: sub-question 1 — Архитектурные различия SGLang и vLLM", started=2.0)
    db.call("s1", "c1", "one")
    db.call("s2", "c2", "two")
    got = {e[2]["id"]: e[0] for e in of(t.poll(), "tool_call")}
    assert got == {"c1": "sub-ff-1", "c2": "sub-ff-2"}


def test_synthetic_event_adopts_the_earliest_unmatched_session(db):
    t = SubagentTail(db.path, LEAD)
    db.session("s1aaaaaaaa", "goal one", started=1.0)
    db.session("s2bbbbbbbb", "goal two", started=2.0)
    t.poll()
    alias = t.adopt("sub-gg-1", "call-G", 0)
    assert alias == [("sub-gg-1", "note", {"kind": "alias", "from": "sub-db-aaaaaaaa", "to": "sub-gg-1"})]
    assert t.take_synthetic_done("sub-gg-1") is False  # covered: the database closes it
    db.call("s1aaaaaaaa", "c1", "x")
    assert of(t.poll(), "tool_call")[0][0] == "sub-gg-1"


def test_task_text_written_after_the_session_row_still_matches(db):
    """30.09: the session row came before its first user message; the empty text
    parked researcher 2 under sub-db-* for the whole job and its task stayed empty."""
    t = tail(db)
    db.session("s1", None)
    assert t.poll() == []                       # held, not a provisional agent
    db.msg("s1", "user", "Researcher 2: AI PDLC\ncontext")
    db.call("s1", "c1", "python3 search.py --query pdlc")
    ev = t.poll()
    assert not of(ev, "spawn") and not of(ev, "note")
    assert of(ev, "tool_call")[0][0] == "sub-aa-2"


def test_hold_gives_up_and_shows_the_session(db):
    t = tail(db)
    db.session("s1abcdefgh", None)
    for _ in range(t.hold_polls - 1):
        assert t.poll() == []
    ev = t.poll()
    assert of(ev, "spawn")[0][0] == "sub-db-abcdefgh"


def test_goal_cut_mid_word_by_the_preview_matches(db):
    t = SubagentTail(db.path, LEAD)
    t.register_spawn("sub-dd-2", "Researcher 2: Бенчмарки производительности SGLang vs vLLM дл", "call-D", 1)
    db.session("s1", "Researcher 2: Бенчмарки производительности SGLang vs vLLM для MoE моделей")
    db.call("s1", "c1", "x")
    ev = t.poll()
    assert of(ev, "tool_call")[0][0] == "sub-dd-2" and not of(ev, "spawn")


def test_one_free_task_and_one_free_session_are_paired(db):
    t = SubagentTail(db.path, LEAD)
    t.register_spawn("sub-ee-1", "Researcher 1: alpha", "call-E", 0)
    t.register_spawn("sub-ee-2", "Researcher 2: beta", "call-E", 1)
    db.session("s1", "Researcher 1: alpha")
    db.session("s2", "a goal the lead rewrote before sending")
    db.call("s2", "c2", "y")
    ev = t.poll()
    assert of(ev, "tool_call", "sub-ee-2") and not of(ev, "spawn")
