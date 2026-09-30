"""Which model and reasoning mode each agent role of a job runs on.

Roles: lead (the orchestrator, and the only agent of a quick job), researcher,
critic, fact_checker. A job's choice travels to Hermes as SEARCHARVESTER_ROLES
(docker/searcharvester_roles.py reads it there). The UI offers the models of
the LLM gateway: GET {OPENAI_BASE_URL}/models, chat models with tool calls
only, cached for a few minutes.

Reasoning modes: auto sends nothing (the model and the gateway decide), off
asks the chat template to skip thinking, low/medium/high set reasoning_effort.
A gateway may pin the mode by model name: on NeuralDeep a `-noreason` alias
never thinks and a base Qwen always does. The catalog's `reasoning` flag tells
the UI which case it is.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROLES = ("lead", "researcher", "critic", "fact_checker")
REASONING = ("auto", "off", "low", "medium", "high")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
ENV = "SEARCHARVESTER_ROLES"


def hermes_default_model(hermes_home: str | Path) -> str | None:
    """model.default from HERMES_HOME/config.yaml: what every role runs on
    unless the job says otherwise."""
    try:
        import yaml
        cfg = yaml.safe_load((Path(hermes_home) / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    m = cfg.get("model")
    if isinstance(m, dict):
        m = m.get("default") or m.get("model")
    return (str(m).strip() or None) if m else None


def defaults(hermes_home: str | Path) -> dict[str, dict[str, str | None]]:
    model = hermes_default_model(hermes_home)
    return {r: {"model": model, "reasoning": "auto"} for r in ROLES}


def resolve(requested: dict[str, Any] | None, base: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The job's roles: the request's fields over the defaults, role by role."""
    out = {r: dict(base.get(r) or {"model": None, "reasoning": "auto"}) for r in ROLES}
    for role, spec in (requested or {}).items():
        if role not in out or not isinstance(spec, dict):
            continue
        if spec.get("model"):
            out[role]["model"] = spec["model"]
        if spec.get("reasoning"):
            out[role]["reasoning"] = spec["reasoning"]
    return out


def to_env(roles: dict[str, dict[str, Any]]) -> str:
    return json.dumps(roles, ensure_ascii=False, separators=(",", ":"))


class ModelCatalog:
    """Chat models with tool calls of the gateway, for the UI pickers."""

    def __init__(self, base_url: str | None, api_key: str | None, ttl_s: float = 300) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._ttl = ttl_s
        self._cached: list[dict[str, Any]] = []
        self._at = 0.0
        self.error: str | None = None

    @classmethod
    def from_env(cls) -> "ModelCatalog":
        return cls(os.environ.get("OPENAI_BASE_URL"), os.environ.get("OPENAI_API_KEY"))

    def models(self) -> list[dict[str, Any]]:
        if self._cached and time.monotonic() - self._at < self._ttl:
            return self._cached
        if not self._base_url:
            self.error = "OPENAI_BASE_URL is not set"
            return self._cached
        try:
            req = urllib.request.Request(f"{self._base_url}/models",
                                         headers={"Authorization": f"Bearer {self._api_key}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            # A stale list beats none; the next call retries.
            self.error = f"model list unavailable: {type(e).__name__}"
            logger.warning("GET %s/models failed: %s", self._base_url, e)
            return self._cached
        self._cached = parse_models(data)
        self._at = time.monotonic()
        self.error = None
        return self._cached

    def ids(self) -> set[str]:
        return {m["id"] for m in self.models()}


def parse_models(data: Any) -> list[dict[str, Any]]:
    """OpenAI /models to [{id, reasoning, context}]. Gateways without the
    capability fields (plain vLLM, OpenRouter) keep every model, reasoning None."""
    rows = data.get("data") if isinstance(data, dict) else None
    out = []
    for m in rows if isinstance(rows, list) else []:
        mid = m.get("id") if isinstance(m, dict) else None
        if not isinstance(mid, str) or not MODEL_ID_RE.match(mid):
            continue
        caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
        if m.get("type") not in (None, "chat") or caps.get("tools") is False:
            continue
        reasoning = caps.get("reasoning", m.get("reasoning"))
        limit = m.get("limit") if isinstance(m.get("limit"), dict) else {}
        out.append({"id": mid, "reasoning": reasoning if isinstance(reasoning, bool) else None,
                    "context": limit.get("context") if isinstance(limit.get("context"), int) else None})
    return sorted(out, key=lambda x: x["id"])
