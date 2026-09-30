"""Per-role models: the adapter side (roles.py, POST /research, GET /research/models)
and the Hermes side (docker/searcharvester_roles.py, patched into the image)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import main
import roles

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
import searcharvester_roles as hr  # noqa: E402

GATEWAY = {"data": [
    {"id": "qwen3.8-27b", "type": "chat", "capabilities": {"tools": True, "reasoning": True}, "limit": {"context": 262144}},
    {"id": "qwen3.6-35b-a3b-noreason", "type": "chat", "capabilities": {"tools": True, "reasoning": False}},
    {"id": "bge-m3", "type": "embedding"},
    {"id": "no-tools", "type": "chat", "capabilities": {"tools": False}},
    {"id": "plain-vllm-model"},
]}


# ---------- Hermes side ----------

@pytest.mark.parametrize("goal,role", [
    ("Researcher: sub-question 1 — benchmarks", "researcher"),
    ("Researcher 2: AI PDLC", "researcher"),
    ("Critic: attack the researchers' conclusions", "critic"),
    ("Fact-checker: verify specific claims", "fact_checker"),
    ("**Fact checker**: verify", "fact_checker"),
    ("Summarise the notes", None),
    ("", None),
])
def test_role_of_goal_prefix(goal, role):
    assert hr.role_of(goal) == role


def test_child_model_by_role_and_fallback(monkeypatch):
    monkeypatch.setenv(hr.ENV, json.dumps({"researcher": {"model": "fast"}, "critic": {"model": ""}}))
    assert hr.child_model("Researcher: x", "default") == "fast"
    assert hr.child_model("Critic: x", "default") == "default"       # empty model = default
    assert hr.child_model("Fact-checker: x", None) is None           # no spec = Hermes inherits
    assert hr.child_model("no prefix", "default") == "default"


def test_broken_env_means_defaults(monkeypatch):
    monkeypatch.setenv(hr.ENV, "{not json")
    assert hr.model_for("lead") is None
    assert hr.apply_overrides(MagicMock(_sh_role="lead"), {"a": 1}) == {"a": 1}


@pytest.mark.parametrize("mode,extras", [
    ("auto", {}), (None, {}),
    ("off", {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}),
    ("low", {"reasoning_effort": "low"}), ("high", {"reasoning_effort": "high"}),
])
def test_request_extras(mode, extras):
    assert hr.request_extras(mode) == extras


class _Agent:
    def __init__(self, depth=0, role=None):
        self._delegate_depth = depth
        if role:
            self._sh_role = role


def test_apply_overrides_by_agent_role(monkeypatch):
    monkeypatch.setenv(hr.ENV, json.dumps({"lead": {"reasoning": "high"}, "critic": {"reasoning": "off"}}))
    assert hr.apply_overrides(_Agent(), {}) == {"reasoning_effort": "high"}           # depth 0 = lead
    base = {"extra_body": {"chat_template_kwargs": {"x": 1}, "tags": ["a"]}}
    out = hr.apply_overrides(_Agent(1, "critic"), base)
    assert out["extra_body"] == {"chat_template_kwargs": {"x": 1, "enable_thinking": False}, "tags": ["a"]}
    assert base["extra_body"]["chat_template_kwargs"] == {"x": 1}                      # input untouched
    assert hr.apply_overrides(_Agent(1, "subagent"), {"k": 1}) == {"k": 1}            # unknown role
    assert hr.apply_overrides(_Agent(1), {"k": 1}) == {"k": 1}                        # a child is never the lead


def test_hermes_image_is_patched():
    """Inside the adapter image: every patch site calls the roles module."""
    files = ["/opt/hermes/acp_adapter/session.py", "/opt/hermes/tools/delegate_tool.py", "/opt/hermes/agent/fast_mode.py"]
    if not all(Path(f).exists() for f in files):
        pytest.skip("not the adapter image")
    for f in files:
        assert "searcharvester_roles" in Path(f).read_text(encoding="utf-8"), f


def test_patched_overrides_work_from_any_cwd(tmp_path):
    """The real Hermes function, run the way a job runs it: another cwd, no
    /opt/hermes on sys.path. A failed import there would break every LLM call."""
    import subprocess
    if not Path("/opt/hermes/agent/fast_mode.py").exists():
        pytest.skip("not the adapter image")
    code = (
        "import sys; sys.path[:] = [p for p in sys.path if p not in ('', '/opt/hermes')]\n"
        "from agent.fast_mode import effective_request_overrides as f\n"
        "class A: pass\n"
        "a = A(); a._delegate_depth = 0; a.request_overrides = {}\n"
        "c = A(); c._delegate_depth = 1; c._sh_role = 'critic'; c.request_overrides = {'extra_body': {'tags': ['x']}}\n"
        "print(f(a)); print(f(c))\n"
    )
    env = {"SEARCHARVESTER_ROLES": json.dumps({"lead": {"reasoning": "high"}, "critic": {"reasoning": "off"}}),
           "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    lead, critic = out.stdout.strip().splitlines()[-2:]
    assert lead == "{'reasoning_effort': 'high'}"
    assert critic == "{'extra_body': {'tags': ['x'], 'chat_template_kwargs': {'enable_thinking': False}}}"


# ---------- adapter side ----------

def test_parse_models_keeps_chat_models_with_tools():
    assert roles.parse_models(GATEWAY) == [
        {"id": "plain-vllm-model", "reasoning": None, "context": None},
        {"id": "qwen3.6-35b-a3b-noreason", "reasoning": False, "context": None},
        {"id": "qwen3.8-27b", "reasoning": True, "context": 262144},
    ]
    assert roles.parse_models({"data": "junk"}) == [] and roles.parse_models(None) == []


def test_resolve_overlays_request_on_defaults():
    base = {r: {"model": "m0", "reasoning": "auto"} for r in roles.ROLES}
    out = roles.resolve({"critic": {"model": "m1"}, "lead": {"reasoning": "low"}, "bogus": {"model": "x"}}, base)
    assert out["critic"] == {"model": "m1", "reasoning": "auto"}
    assert out["lead"] == {"model": "m0", "reasoning": "low"}
    assert set(out) == set(roles.ROLES)


def test_defaults_from_hermes_config(tmp_path):
    (tmp_path / "config.yaml").write_text('model:\n  default: "qwen3.6-35b-a3b-noreason"\n  provider: custom\n')
    assert roles.defaults(tmp_path)["fact_checker"] == {"model": "qwen3.6-35b-a3b-noreason", "reasoning": "auto"}
    assert roles.hermes_default_model(tmp_path / "missing") is None


def test_catalog_keeps_the_last_list_when_the_gateway_fails(monkeypatch):
    cat = roles.ModelCatalog("http://gw/v1", "k", ttl_s=0)
    monkeypatch.setattr(roles.urllib.request, "urlopen", MagicMock(side_effect=OSError("down")))
    assert cat.models() == [] and cat.error
    cat._cached = [{"id": "a", "reasoning": None, "context": None}]
    assert cat.ids() == {"a"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    (tmp_path / "config.yaml").write_text("model:\n  default: base-model\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    orch = MagicMock()
    orch.spawn = AsyncMock(return_value="abcdef0123456789")
    monkeypatch.setattr(main, "orchestrator", orch)
    cat = MagicMock()
    cat.models = MagicMock(return_value=roles.parse_models(GATEWAY))
    cat.ids = MagicMock(return_value={m["id"] for m in roles.parse_models(GATEWAY)})
    cat.error = None
    monkeypatch.setattr(main, "model_catalog", cat)
    return TestClient(main.app, base_url="http://localhost", headers={"X-Searcharvester-Client": "1"}), orch, cat


def test_models_endpoint(client):
    c, _, _ = client
    r = c.get("/research/models")
    assert r.status_code == 200
    d = r.json()
    assert d["roles"] == ["lead", "researcher", "critic", "fact_checker"]
    assert d["defaults"]["lead"] == {"model": "base-model", "reasoning": "auto"}
    assert [m["id"] for m in d["models"]] == ["plain-vllm-model", "qwen3.6-35b-a3b-noreason", "qwen3.8-27b"]


def test_post_research_passes_resolved_roles(client):
    c, orch, _ = client
    r = c.post("/research", json={"query": "q", "models": {
        "lead": {"model": "qwen3.8-27b", "reasoning": "low"}, "researcher": {"model": "qwen3.6-35b-a3b-noreason"}}})
    assert r.status_code == 202
    models = orch.spawn.await_args.kwargs["models"]
    assert models["lead"] == {"model": "qwen3.8-27b", "reasoning": "low"}
    assert models["researcher"] == {"model": "qwen3.6-35b-a3b-noreason", "reasoning": "auto"}
    assert models["critic"] == {"model": "base-model", "reasoning": "auto"}


@pytest.mark.parametrize("body", [
    {"models": {"lead": {"model": "gpt-9-imaginary"}}},       # not served by the gateway
    {"models": {"lead": {"model": "bad model; rm -rf"}}},     # not a model id
    {"models": {"lead": {"reasoning": "extreme"}}},
    {"models": {"janitor": {"model": "qwen3.8-27b"}}},
])
def test_post_research_rejects_bad_roles(client, body):
    c, orch, _ = client
    assert c.post("/research", json={"query": "q", **body}).status_code == 422
    orch.spawn.assert_not_awaited()


def test_post_research_without_a_model_list_skips_the_check(client):
    c, orch, cat = client
    cat.ids.return_value = set()
    assert c.post("/research", json={"query": "q", "models": {"lead": {"model": "anything-new"}}}).status_code == 202
