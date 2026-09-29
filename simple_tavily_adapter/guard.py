"""Loop guard for one research job: the whole flow, lead and sub-agents together.

Hermes guards a single agent turn by itself (identical calls, call cycles, the same
failure over and over; config `tool_loop_guardrails`, hard stops switched on in
hermes-data/config.yaml). What it cannot see is the job as a whole: five
sub-agents that each stay under their own caps can still burn 300 LLM calls,
repeat each other's searches or wait forever on a stuck child. This guard sees
all of it through three feeds:

- hermes.log lines (every agent logs "API call #N ... in=X out=Y" and failed tools);
- the lead's ACP events (its streamed message text);
- /search and /extract requests the skill scripts tag with the job id.

It answers with signals, the orchestrator acts on them:

- warn  at `warn_ratio` of a budget: a note event, nothing is stopped
        (queued, the orchestrator reads them with pending_warnings());
- soft  for search/extract over budget or a repeated query: the tool call gets an
        empty result with a notice telling the agent to write from what it has;
- stop  for LLM calls or input tokens over budget, the same error repeating,
        degenerate (looping) message text or no sign of life for `stall_s`: the
        orchestrator cancels the turn and gives the lead one short wrap-up turn.

Limits come from GUARD_* env vars, defaults fit one deep research on our lanes.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

_API_CALL_RE = re.compile(r"API call #\d+:.*?\bin=(\d+)\s+out=(\d+)")
_TOOL_ERROR_RE = re.compile(r"Tool (\S+) returned error \([^)]*\):\s*(.*)")
_NOISE_RE = re.compile(r"\d+|/[\w./-]+|0x[0-9a-f]+", re.I)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    max_llm_calls: int = 150
    max_input_tokens: int = 3_000_000
    max_searches: int = 40
    max_extracts: int = 60
    same_error_stop: int = 12      # across all agents; one agent is guarded by Hermes itself
    stall_s: int = 420
    warn_ratio: float = 0.8
    repeat_line_stop: int = 4      # the same long line this many times in the lead's text
    wrapup_s: int = 240            # the lead's last turn after a stop

    @classmethod
    def quick(cls) -> "Limits":
        """depth=quick: one agent answering a short question. GUARD_QUICK_* env overrides."""
        base = cls.from_env()
        return cls(
            max_llm_calls=_env_int("GUARD_QUICK_MAX_LLM_CALLS", 30),
            max_input_tokens=_env_int("GUARD_QUICK_MAX_INPUT_TOKENS", 800_000),
            max_searches=_env_int("GUARD_QUICK_MAX_SEARCHES", 8),
            max_extracts=_env_int("GUARD_QUICK_MAX_EXTRACTS", 8),
            same_error_stop=6, stall_s=base.stall_s, repeat_line_stop=base.repeat_line_stop,
            wrapup_s=120,
        )

    @classmethod
    def from_env(cls) -> "Limits":
        d = cls()
        return cls(
            max_llm_calls=_env_int("GUARD_MAX_LLM_CALLS", d.max_llm_calls),
            max_input_tokens=_env_int("GUARD_MAX_INPUT_TOKENS", d.max_input_tokens),
            max_searches=_env_int("GUARD_MAX_SEARCHES", d.max_searches),
            max_extracts=_env_int("GUARD_MAX_EXTRACTS", d.max_extracts),
            same_error_stop=_env_int("GUARD_SAME_ERROR_STOP", d.same_error_stop),
            stall_s=_env_int("GUARD_STALL_S", d.stall_s),
            repeat_line_stop=_env_int("GUARD_REPEAT_LINE_STOP", d.repeat_line_stop),
            wrapup_s=_env_int("GUARD_WRAPUP_S", d.wrapup_s),
        )


@dataclass(frozen=True)
class Signal:
    level: str   # warn | soft | stop
    reason: str

    def to_payload(self) -> dict:
        return {"kind": "guard", "action": self.level, "reason": self.reason}


def _norm_query(q: str) -> str:
    return " ".join(re.findall(r"\w+", q.lower()))


def _norm_url(u: str) -> str:
    u = u.strip().split("#", 1)[0].rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", u, flags=re.I).lower()


@dataclass
class JobGuard:
    limits: Limits = field(default_factory=Limits.from_env)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    searches: int = 0
    extracts: int = 0
    duplicates: int = 0
    wrapup: bool = False              # after a stop: tools are closed, only writing is left
    tripped: Optional[Signal] = None  # the first stop signal, kept for the final status
    last_activity: float = field(default_factory=time.monotonic)
    _errors: Counter = field(default_factory=Counter)
    _queries: dict = field(default_factory=dict)   # normalized query -> cached response
    _urls: set = field(default_factory=set)
    _warned: set = field(default_factory=set)
    _pending: list = field(default_factory=list)   # warn signals not yet emitted
    _text: str = ""

    # ---------- feeds ----------

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def on_log_line(self, line: str) -> list[Signal]:
        self.touch()
        out: list[Signal] = []
        m = _API_CALL_RE.search(line)
        if m:
            self.llm_calls += 1
            self.input_tokens += int(m.group(1))
            self.output_tokens += int(m.group(2))
            out += self._budget("llm_calls", self.llm_calls, self.limits.max_llm_calls)
            out += self._budget("input_tokens", self.input_tokens, self.limits.max_input_tokens)
        m = _TOOL_ERROR_RE.search(line)
        if m:
            key = f"{m.group(1)}: {_NOISE_RE.sub('#', m.group(2))[:160]}"
            self._errors[key] += 1
            if self._errors[key] == self.limits.same_error_stop:
                out.append(self._stop(f"the same tool error {self._errors[key]} times: {key[:120]}"))
        return out

    def on_message(self, text: str) -> list[Signal]:
        """The lead's streamed text. A model stuck in a loop repeats whole lines."""
        self.touch()
        self._text = (self._text + text)[-20000:]
        lines = [ln.strip() for ln in self._text.splitlines() if len(ln.strip()) >= 40]
        if not lines:
            return []
        line, n = Counter(lines).most_common(1)[0]
        if n >= self.limits.repeat_line_stop:
            return [self._stop(f"looping text, one line repeated {n} times: {line[:80]}")]
        return []

    def on_search(self, query: str) -> tuple[str, Optional[str], Optional[dict]]:
        """(verdict, notice, cached): verdict ok | duplicate | exhausted."""
        self.touch()
        if self.wrapup:
            return "exhausted", "Research is being wrapped up: no more searches. Write report.md now.", None
        key = _norm_query(query)
        if key in self._queries:
            self.duplicates += 1
            return ("duplicate", "This query was already run in this research; same results as before. "
                    "Use them or search for something new.", self._queries[key])
        if self.searches >= self.limits.max_searches:
            return ("exhausted", f"The search budget of this research is spent ({self.limits.max_searches}). "
                    "Stop searching and work with the sources you already have.", None)
        self.searches += 1
        self._warn_once("searches", self.searches, self.limits.max_searches)
        return "ok", None, None

    def remember_search(self, query: str, response: dict) -> None:
        self._queries[_norm_query(query)] = response

    def on_extract(self, url: str) -> tuple[str, Optional[str]]:
        self.touch()
        if self.wrapup:
            return "exhausted", "Research is being wrapped up: no more page reads. Write report.md now."
        key = _norm_url(url)
        if key in self._urls:
            self.duplicates += 1
            return "duplicate", None   # the extract cache answers it, it costs nothing
        if self.extracts >= self.limits.max_extracts:
            return ("exhausted", f"The page reading budget of this research is spent ({self.limits.max_extracts}). "
                    "Use the pages already saved in extracts/.")
        self._urls.add(key)
        self.extracts += 1
        self._warn_once("extracts", self.extracts, self.limits.max_extracts)
        return "ok", None

    def check_idle(self, now: Optional[float] = None) -> Optional[Signal]:
        now = time.monotonic() if now is None else now
        idle = now - self.last_activity
        if idle >= self.limits.stall_s:
            return self._stop(f"no activity for {int(idle)} s")
        return None

    # ---------- state ----------

    def pending_warnings(self) -> list[Signal]:
        out, self._pending = self._pending, []
        return out

    def stats(self) -> dict:
        return {"llm_calls": self.llm_calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "searches": self.searches,
                "extracts": self.extracts, "duplicates": self.duplicates}

    def _budget(self, name: str, value: int, limit: int) -> list[Signal]:
        if value >= limit:
            return [self._stop(f"{name} budget reached: {value} of {limit}")]
        self._warn_once(name, value, limit)
        return []

    def _warn_once(self, name: str, value: int, limit: int) -> None:
        """Queue one warning per budget at warn_ratio; read by pending_warnings()."""
        if name not in self._warned and value >= limit * self.limits.warn_ratio:
            self._warned.add(name)
            self._pending.append(Signal("warn", f"{name} at {value} of {limit}"))

    def _stop(self, reason: str) -> Signal:
        sig = Signal("stop", reason)
        if self.tripped is None:
            self.tripped = sig
        return sig
