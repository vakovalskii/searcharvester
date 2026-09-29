"""Patches applied to the pinned Hermes image at build time.

Streaming: the vLLM streaming tool_call bug (vllm#27641) routes gpt-oss tool-call
JSON into reasoning_content when stream=true, and Hermes thinks the model is
narrating instead of calling tools. Since v0.21 the decision lives in
agent/turn_api_call.py:_should_stream(); make it honour HERMES_DISABLE_STREAMING
(default "1" here) instead of patching a literal. Fails the build loudly when the
anchor moves, so a Hermes bump cannot silently drop the patch.
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
