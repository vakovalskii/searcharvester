"""Per-role model and reasoning for one research job, read inside Hermes.

The adapter puts the job's choice into SEARCHARVESTER_ROLES (JSON) of the
`hermes acp` process, e.g.

    {"lead": {"model": "qwen3.8-27b", "reasoning": "low"},
     "researcher": {"model": "qwen3.6-35b-a3b-noreason", "reasoning": "auto"},
     "critic": {...}, "fact_checker": {...}}

patch_hermes.py wires three spots of Hermes to this module:

- the ACP session builds the lead with model_for("lead");
- delegate_task builds a child with child_model(goal, default): the role comes
  from the goal prefix the deep-research skill requires ("Researcher: ...",
  "Critic: ...", "Fact-checker: ..."), and the child remembers it as `_sh_role`;
- every LLM request of an agent passes through apply_overrides(), which adds
  the reasoning fields of that agent's role.

Nothing here raises: a broken or missing variable means Hermes defaults.
Copied to /opt/hermes at image build, so stdlib only.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

ROLES = ("lead", "researcher", "critic", "fact_checker")
REASONING = ("auto", "off", "low", "medium", "high")
ENV = "SEARCHARVESTER_ROLES"

_GOAL_ROLE = re.compile(r"^\W*(researcher|critic|fact[\s_-]?checker)\b", re.I)


def _specs() -> dict[str, Any]:
    try:
        d = json.loads(os.environ.get(ENV) or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def role_of(goal: Any) -> str | None:
    """Role of a sub-agent by its goal prefix, None when the goal has none."""
    m = _GOAL_ROLE.match(str(goal or ""))
    if not m:
        return None
    word = m.group(1).lower()
    return "fact_checker" if word.startswith("fact") else word


def spec(role: str | None) -> dict[str, Any]:
    s = _specs().get(role) if role else None
    return s if isinstance(s, dict) else {}


def model_for(role: str | None) -> str | None:
    m = spec(role).get("model")
    return m.strip() if isinstance(m, str) and m.strip() else None


def child_model(goal: Any, default: str | None) -> str | None:
    return model_for(role_of(goal)) or default


def request_extras(reasoning: Any) -> dict[str, Any]:
    """Request fields for a reasoning mode. `auto` sends nothing: the model and
    the gateway decide. `off` is the Qwen/vLLM chat-template switch; a gateway
    may still force thinking for a model, a `-noreason` alias is the sure way."""
    r = str(reasoning or "auto").lower()
    if r == "off":
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    if r in ("low", "medium", "high"):
        return {"reasoning_effort": r}
    return {}


def agent_role(agent: Any) -> str | None:
    role = getattr(agent, "_sh_role", None)
    if role:
        return role
    return "lead" if not getattr(agent, "_delegate_depth", 0) else None


def apply_overrides(agent: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    """Hermes' request overrides of one agent plus the reasoning of its role."""
    try:
        extras = request_extras(spec(agent_role(agent)).get("reasoning"))
    except Exception:
        return overrides
    if not extras:
        return overrides
    out = dict(overrides)
    for k, v in extras.items():
        if k == "extra_body":
            body = dict(out.get("extra_body") or {})
            for bk, bv in v.items():
                body[bk] = {**body[bk], **bv} if isinstance(body.get(bk), dict) and isinstance(bv, dict) else bv
            out["extra_body"] = body
        else:
            out[k] = v
    return out
