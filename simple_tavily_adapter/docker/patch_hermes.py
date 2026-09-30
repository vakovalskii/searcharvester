"""Patches applied to the pinned Hermes image at build time.

Streaming: the vLLM streaming tool_call bug (vllm#27641) routes gpt-oss tool-call
JSON into reasoning_content when stream=true, and Hermes thinks the model is
narrating instead of calling tools. Since v0.21 the decision lives in
agent/turn_api_call.py:_should_stream(); make it honour HERMES_DISABLE_STREAMING
(default "1" here) instead of patching a literal. Fails the build loudly when the
anchor moves, so a Hermes bump cannot silently drop the patch.

Iteration budget: upstream's checkpoint notice says "continue the task; do not
stop", so a researcher keeps reading until the hard cap, and the toolless
summary call after it often comes back empty ("I reached the iteration limit
and couldn't generate a summary"). For research the last steps must go to
writing, so the notice tells the agent to stop gathering and answer.
"""

PATH = "/opt/hermes/agent/turn_api_call.py"
OLD = '    if getattr(agent, "_disable_streaming", False):\n        return False\n'
NEW = OLD + (
    '    import os as _os\n'
    '    if _os.environ.get("HERMES_DISABLE_STREAMING", "1") == "1":\n'
    '        return False\n'
)

src = open(PATH, encoding="utf-8").read()
assert src.count(OLD) == 1, "_should_stream anchor not found: upstream changed, update docker/patch_hermes.py"
open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW))
print("patched _should_stream: HERMES_DISABLE_STREAMING")

BUDGET_PATH = "/opt/hermes/agent/turn_iteration_prep.py"
BUDGET_OLD = (
    '    "iterations. Checkpoint durable progress now, then continue the task; do not stop "\n'
    '    "solely because of this warning."\n'
)
BUDGET_NEW = (
    '    "iterations. Stop searching and opening new pages now. In your next reply write "\n'
    '    "your final answer from what you already have: findings with their source URLs, "\n'
    '    "and what stays unverified. Use at most one more tool call, only if it is essential."\n'
)
src = open(BUDGET_PATH, encoding="utf-8").read()
assert src.count(BUDGET_OLD) == 1, "budget notice anchor not found: upstream changed, update docker/patch_hermes.py"
open(BUDGET_PATH, "w", encoding="utf-8").write(src.replace(BUDGET_OLD, BUDGET_NEW))
print("patched iteration budget notice: wrap up instead of continue")
