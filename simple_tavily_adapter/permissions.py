"""Answer Hermes permission requests coming over ACP.

Since Hermes v0.21 every file edit (and some risky actions) asks the ACP client
for approval. The old client answered "method not found", Hermes treated that as
a denial, write_file failed and the agent looped on retries (plan.md, report.md).

Policy: a research agent may write only inside its own job workspace. An edit is
allowed when every path it touches resolves inside the workspace; anything else
(paths outside, requests we cannot read a path from, non-edit actions) is denied.
The decision is returned together with a short reason for the event stream.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

_PATH_KEYS = ("path", "file_path", "filepath", "filename", "target", "destination")


def _paths_from(obj: Any) -> Iterable[str]:
    if obj is None:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _PATH_KEYS and isinstance(v, str):
                yield v
            elif isinstance(v, (dict, list, tuple)):
                yield from _paths_from(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _paths_from(v)
    else:
        for attr in ("path", "locations", "raw_input", "rawInput"):
            if hasattr(obj, attr):
                yield from _paths_from(getattr(obj, attr))


def _as_plain(tool_call: Any) -> dict:
    if hasattr(tool_call, "model_dump"):
        return tool_call.model_dump(by_alias=False)
    return tool_call if isinstance(tool_call, dict) else {}


def inside(workspace: Path, candidate: str) -> bool:
    ws = workspace.resolve()
    p = Path(candidate)
    p = (ws / p) if not p.is_absolute() else p
    try:
        real = Path(os.path.realpath(p))
    except OSError:
        return False
    return real == ws or ws in real.parents


def decide(workspace: Path, tool_call: Any) -> tuple[bool, str]:
    """(allow, reason). Only edits with every path inside the workspace pass."""
    plain = _as_plain(tool_call)
    kind = str(plain.get("kind") or "").lower()
    paths = sorted(set(_paths_from(plain)))
    if kind not in ("edit", "write", "delete", "move", ""):
        return False, f"kind {kind} is not an edit"
    if not paths:
        return False, "no path in the request"
    outside = [p for p in paths if not inside(workspace, p)]
    if outside:
        return False, f"outside the job workspace: {outside[0]}"
    return True, f"edit inside the workspace: {', '.join(paths)[:200]}"


def pick_option(options: Iterable[Any], allow: bool) -> str | None:
    """option_id to answer with: allow_once when allowed, a reject option otherwise."""
    opts = list(options or [])
    wanted = ("allow_once",) if allow else ("reject_once", "reject_always")
    for o in opts:
        if str(getattr(o, "kind", "")) in wanted or getattr(o, "option_id", "") in wanted:
            return o.option_id
    return None
