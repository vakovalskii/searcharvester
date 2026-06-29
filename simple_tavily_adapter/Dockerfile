FROM nousresearch/hermes-agent:latest

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl gosu \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --no-cache-dir --python /opt/hermes/.venv/bin/python3 \
    -r /tmp/requirements.txt

# Workaround for vLLM streaming tool_call bug (vllm#27641, fix in PR #35449).
# When stream=true, large prompts route tool-call JSON into reasoning_content
# instead of the structured tool_calls array, so Hermes thinks the model is
# narrating instead of calling tools. Non-streaming works fine.
# Only needed when using a vLLM backend. Remove this block if using OpenAI /
# Anthropic / OpenRouter.
# Note: logic moved from run_agent.py to agent/conversation_loop.py in a recent
# hermes-agent upstream update.
RUN python3 - <<'PY'
path = '/opt/hermes/agent/conversation_loop.py'
src = open(path, encoding='utf-8').read()
old = '_use_streaming = True\n                # Provider signaled'
new = '_use_streaming = False  # patched: vLLM streaming tool_call bug workaround (vllm#27641)\n                # Provider signaled'
assert old in src, 'streaming toggle anchor not found — upstream changed; update the patch'
open(path, 'w', encoding='utf-8').write(src.replace(old, new))
print('patched _use_streaming = False')
PY

RUN mkdir -p /app && chown hermes:hermes /app
COPY --chown=hermes:hermes . /app/

COPY --chown=root:root docker/entrypoint-adapter.sh /entrypoint-adapter.sh
RUN chmod +x /entrypoint-adapter.sh

WORKDIR /app
EXPOSE 8000

ENTRYPOINT ["/entrypoint-adapter.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
