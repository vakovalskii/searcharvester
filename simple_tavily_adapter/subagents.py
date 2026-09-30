"""Live sub-agent steps from Hermes' state.db (A2 in docs/ui-and-visualization.md).

Over ACP only the lead streams. Hermes v0.21 keeps every session in
HERMES_HOME/state.db (the old sessions/*.json files are gone): sub-agents are rows
in `sessions` with `parent_session_id` = the lead's ACP session id, their steps are
rows in `messages`. This module tails those rows and turns them into events with
the SAME agent id events.py gives a delegate_task task (`_sub_agent_id`), so the UI
sees one agent per task whichever producer spoke.

Reconciliation, one terminal event per task:
- register_spawn() is called for every synthetic spawn from delegate_task;
- a session is matched to an uncovered task by its first user message (the task
  goal, the lead may prefix context), ties broken by start order within a batch;
- an unmatched session gets a provisional id `sub-db-<8>`; every poll retries the
  match and, once found, emits `note kind: alias {from, to}`;
- a covered task gets `done` from the database when the session ends, and the
  synthetic `done` from the delegate result is suppressed; an uncovered task keeps
  the synthetic one (take_synthetic_done decides);
- finish() closes every task still open with `done {status: unknown}`.

The database is opened with a normal connection plus `PRAGMA query_only`: a
`mode=ro` connection fails on a WAL database whose -shm file is missing. Any
sqlite error only skips the tick; a missing table or column switches the tail
off (available = False) and the synthetic events carry on alone.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_TEXT = 2000


def _goal_key(goal: str) -> str:
    """The part of a task goal that must appear in the session's first message. The
    ACP preview cuts goals mid-word and may add an ellipsis: drop both."""
    g = " ".join(goal.split())[:200].rstrip(".…").rstrip()
    if len(g) >= 40 and " " in g:
        g = g.rsplit(" ", 1)[0]   # the last word may be cut ("дл" of "для")
    return g


@dataclass
class _Task:
    agent_id: str
    goal: str
    call_id: str
    index: int
    session: str | None = None
    done: bool = False


@dataclass
class _Session:
    sid: str
    started_at: float
    agent_id: str | None = None      # canonical once matched
    provisional: str | None = None   # sub-db-xxxx while unmatched
    cursor: int = 0
    usage: tuple = ()
    ended: bool = False
    first_user: str = ""
    empty_polls: int = 0             # polls seen before Hermes wrote the task message

    @property
    def emit_id(self) -> str:
        return self.agent_id or self.provisional or f"sub-db-{self.sid[-8:]}"


@dataclass
class SubagentTail:
    db_path: Path
    lead_session_id: str
    available: bool = True
    hold_polls: int = 5              # ~10 s at the 2 s tail interval
    _tasks: dict[str, _Task] = field(default_factory=dict)
    _sessions: dict[str, _Session] = field(default_factory=dict)
    # Optional columns (model, reasoning) differ between Hermes versions: read
    # the ones this database has, so an older schema keeps the tail on.
    _cols: dict[str, set[str]] | None = None

    # ---------- inputs from the ACP side ----------

    def register_spawn(self, agent_id: str, goal: str, call_id: str, index: int) -> None:
        self._tasks.setdefault(agent_id, _Task(agent_id, goal or "", call_id or "", int(index or 0)))

    def adopt(self, agent_id: str, call_id: str, index: int) -> list[tuple[str, str, dict]]:
        """A synthetic event for a task we never saw spawn (no goals in the ACP
        call): register it and give it the earliest unmatched session, since
        delegation runs batch after batch."""
        if agent_id in self._tasks:
            return []
        t = self._tasks[agent_id] = _Task(agent_id, "", call_id or "", int(index or 0))
        free = sorted((s for s in self._sessions.values() if s.agent_id is None),
                      key=lambda s: (s.started_at, s.sid))
        if not free:
            return []
        s = free[0]
        t.session, s.agent_id = s.sid, agent_id
        if s.ended:
            t.done = True  # its database done went out under the provisional id
        if s.provisional:
            return [(agent_id, "note", {"kind": "alias", "from": s.provisional, "to": agent_id})]
        return []

    def known(self, agent_id: str) -> bool:
        return agent_id in self._tasks

    def take_synthetic_done(self, agent_id: str) -> bool:
        """True: emit the synthetic done from the delegate result; False: drop it."""
        t = self._tasks.get(agent_id)
        if t is None:
            return True
        if t.done:
            return False
        if self.available and t.session is not None:
            return False  # the database closes this one when its session ends
        t.done = True
        return True

    # ---------- polling ----------

    def poll(self) -> list[tuple[str, str, dict]]:
        """New (agent_id, type, payload) triples since the last call."""
        if not self.available:
            return []
        try:
            return self._poll()
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "no such table" in msg or "no such column" in msg:
                self.available = False
            return []
        except sqlite3.DatabaseError:
            return []

    def finish(self) -> list[tuple[str, str, dict]]:
        """Last poll, then close every task and session that is still open."""
        out = self.poll()
        for s in self._sessions.values():
            if not s.ended and s.agent_id is None:
                s.ended = True
                out.append((s.emit_id, "done", {"status": "unknown", "reason": "session did not finish"}))
        for t in self._tasks.values():
            if not t.done:
                t.done = True
                reason = "session did not finish" if t.session else "result not available"
                out.append((t.agent_id, "done", {"status": "unknown", "reason": reason}))
        return out

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.db_path), timeout=0.5)
        db.execute("PRAGMA query_only=ON")
        return db

    def _opt(self, db: sqlite3.Connection, table: str, col: str) -> str:
        """`col` when the table has it, else NULL (same row shape either way)."""
        if self._cols is None:
            cols = {t: {r[1] for r in db.execute(f"PRAGMA table_info({t})")} for t in ("sessions", "messages")}
            if not all(cols.values()):
                return "NULL"   # schema not written yet: ask again next tick
            self._cols = cols
        return col if col in self._cols.get(table, set()) else "NULL"

    def _poll(self) -> list[tuple[str, str, dict]]:
        out: list[tuple[str, str, dict]] = []
        if not self.db_path.exists():
            return out
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT id, started_at, ended_at, end_reason, tool_call_count, input_tokens, output_tokens,"
                f" {self._opt(db, 'sessions', 'model')}, {self._opt(db, 'sessions', 'reasoning_tokens')}"
                " FROM sessions WHERE parent_session_id = ? ORDER BY started_at, id",
                (self.lead_session_id,),
            ).fetchall()
            for sid, started, ended_at, end_reason, tcc, tin, tout, model, treason in rows:
                s = self._sessions.get(sid)
                if s is None:
                    s = self._sessions[sid] = _Session(sid, float(started or 0))
                # The session row can land before its first user message (the task).
                # Matching on an empty text would park it under a provisional id for
                # good, so re-read until it is there and hold its steps meanwhile.
                if not s.first_user and s.agent_id is None:
                    first = db.execute(
                        "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1",
                        (sid,),
                    ).fetchone()
                    s.first_user = (first[0] or "") if first else ""
                    if not s.first_user and not ended_at:
                        s.empty_polls += 1
                        if s.empty_polls < self.hold_polls:
                            continue
                if s.agent_id is None:
                    out += self._match(s)
                out += self._messages(db, s)
                usage = (int(tin or 0), int(tout or 0), int(tcc or 0), int(treason or 0), str(model or ""))
                if usage != s.usage:
                    s.usage = usage
                    pl = {"input_tokens": usage[0], "output_tokens": usage[1], "tool_call_count": usage[2],
                          "reasoning_tokens": usage[3]}
                    if usage[4]:
                        pl["model"] = usage[4]   # what the sub-agent really ran on
                    out.append((s.emit_id, "usage", pl))
                if ended_at and not s.ended:
                    s.ended = True
                    out += self._close(s, end_reason)
        finally:
            db.close()
        return out

    def _match(self, s: _Session) -> list[tuple[str, str, dict]]:
        text = " ".join(s.first_user.split())
        candidates = [t for t in self._tasks.values()
                      if t.session is None and t.goal and _goal_key(t.goal) in text]
        if not candidates and text:
            # One free task and this the only free session with a task text: they
            # are the same agent, whatever the preview did to the goal.
            free_tasks = [t for t in self._tasks.values() if t.session is None]
            free_sessions = [x for x in self._sessions.values() if x.agent_id is None and x.first_user]
            if len(free_tasks) == 1 and free_sessions == [s]:
                candidates = free_tasks
        if not candidates:
            if s.provisional is None:
                s.provisional = f"sub-db-{s.sid[-8:]}"
                return [(s.provisional, "spawn", {"goal": s.first_user[:MAX_TEXT], "unmatched": True})]
            return []
        # Same goal twice: the earliest unmatched task of the earliest batch wins.
        task = sorted(candidates, key=lambda t: (t.call_id, t.index))[0]
        task.session = s.sid
        s.agent_id = task.agent_id
        out: list[tuple[str, str, dict]] = []
        if s.provisional:
            out.append((task.agent_id, "note", {"kind": "alias", "from": s.provisional, "to": task.agent_id}))
        return out

    def _close(self, s: _Session, end_reason: Any) -> list[tuple[str, str, dict]]:
        reason = str(end_reason or "")
        status = "failed" if any(w in reason for w in ("error", "fail", "crash")) else \
            "interrupted" if "interrupt" in reason or "cancel" in reason else "completed"
        if s.agent_id:
            t = self._tasks[s.agent_id]
            if t.done:
                return []
            t.done = True
        return [(s.emit_id, "done", {"status": status, "end_reason": reason})]

    def _messages(self, db: sqlite3.Connection, s: _Session) -> list[tuple[str, str, dict]]:
        out: list[tuple[str, str, dict]] = []
        rows = db.execute(
            "SELECT id, role, content, tool_calls, tool_name, tool_call_id, timestamp,"
            f" {self._opt(db, 'messages', 'reasoning_content')}, {self._opt(db, 'messages', 'reasoning')}"
            " FROM messages WHERE session_id = ? AND id > ? ORDER BY id",
            (s.sid, s.cursor),
        ).fetchall()
        first_user_seen = s.cursor > 0
        for mid, role, content, tool_calls, tool_name, tool_call_id, ts, rcontent, rtext in rows:
            s.cursor = mid
            if role == "user" and not first_user_seen:
                first_user_seen = True  # the goal: already in spawn
                continue
            if role == "assistant":
                thought = rcontent or rtext
                if isinstance(thought, str) and thought.strip():
                    out.append((s.emit_id, "thought", {"text": thought[:MAX_TEXT * 4]}))
                for tc in _tool_calls(tool_calls):
                    out.append((s.emit_id, "tool_call", tc | {"ts_db": ts}))
                if content and content.strip():
                    out.append((s.emit_id, "message", {"text": content[:MAX_TEXT * 4]}))
            elif role == "tool":
                out.append((s.emit_id, "tool_result", _tool_result(tool_call_id, tool_name, content) | {"ts_db": ts}))
        return out


def _tool_calls(raw: Any) -> list[dict]:
    try:
        calls = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except ValueError:
        return []
    out = []
    for c in calls if isinstance(calls, list) else []:
        fn = c.get("function") or {}
        name = fn.get("name") or "tool"
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"raw": str(fn.get("arguments"))[:MAX_TEXT]}
        detail = args.get("command") or args.get("path") or args.get("query") or "" if isinstance(args, dict) else ""
        out.append({"id": c.get("id") or c.get("call_id"), "title": f"{name}: {str(detail)[:120]}".rstrip(": "),
                    "kind": "execute" if name == "terminal" else "other", "raw_input": args, "tool": name})
    return out


def _tool_result(call_id: Any, tool_name: Any, content: Any) -> dict:
    text = content or ""
    status = "completed"
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            if d.get("exit_code") not in (None, 0) or d.get("error"):
                status = "failed"
            text = d.get("output") if isinstance(d.get("output"), str) else text
    except ValueError:
        pass
    return {"id": call_id, "tool": tool_name, "status": status, "content": str(text)[:MAX_TEXT]}
