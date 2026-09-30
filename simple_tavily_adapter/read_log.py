"""One row per page read, in a local sqlite file (READ_LOG_DB).

Same columns as neuraldeep's page_read_log, so the gate can be tuned the same
way: which path answered, why the gate decided so, what the judge said, time of
every step. 90 days are kept. Writes run in a thread; a failed write never breaks
a read.

  sqlite3 jobs/page_read_log.sqlite3 \
    "select path, count(*), round(avg(total_ms)) from page_read_log group by 1"
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from dataclasses import asdict

from reader import Trace

DDL = """
CREATE TABLE IF NOT EXISTS page_read_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  source TEXT, caller TEXT, url TEXT, host TEXT, path TEXT,
  fast_status INTEGER, fast_error TEXT, fast_via_proxy INTEGER, fast_chars INTEGER, fast_ms INTEGER,
  gate_decision TEXT, gate_reason TEXT, extractor TEXT, signals TEXT,
  judge_ok INTEGER, judge_reason TEXT, judge_ms INTEGER,
  reader_called INTEGER, reader_chars INTEGER, reader_ms INTEGER, reader_error TEXT,
  browser_called INTEGER, browser_via_proxy INTEGER, browser_chars INTEGER, browser_ms INTEGER, browser_error TEXT,
  final_chars INTEGER, total_ms INTEGER, snippet TEXT
);
CREATE INDEX IF NOT EXISTS idx_page_read_log_ts ON page_read_log(ts);
CREATE INDEX IF NOT EXISTS idx_page_read_log_host ON page_read_log(host, ts);
"""
RETENTION = "DELETE FROM page_read_log WHERE ts < strftime('%Y-%m-%dT%H:%M:%fZ','now','-90 days')"

_lock = threading.Lock()


def _path() -> str:
    return os.environ.get("READ_LOG_DB", "/srv/searxng-docker/jobs/page_read_log.sqlite3")


def init() -> None:
    with _lock, sqlite3.connect(_path()) as db:
        db.executescript(DDL)
        # logs created before the extractor choice (2026-09-30) lack the column
        if "extractor" not in {r[1] for r in db.execute("PRAGMA table_info(page_read_log)")}:
            db.execute("ALTER TABLE page_read_log ADD COLUMN extractor TEXT")
        db.execute(RETENTION)


def _write(tr: Trace) -> None:
    from urllib.parse import urlparse
    row = asdict(tr)
    row["host"] = (urlparse(tr.url).hostname or "")[:255]
    row["signals"] = json.dumps(tr.signals) if tr.signals else None
    cols = [c for c in row if c != "signals"] + ["signals"]
    with _lock, sqlite3.connect(_path()) as db:
        db.execute(f"INSERT INTO page_read_log ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                   [row[c] for c in cols])


async def log(tr: Trace) -> None:
    await asyncio.to_thread(_write, tr)
