FROM nousresearch/hermes-agent:latest

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

RUN uv pip install --no-cache-dir --python /opt/hermes/.venv/bin/python3 \
    "trafilatura>=1.12,<3.0" \
    "lxml>=4.9" \
    "beautifulsoup4==4.12.2" \
    "sse-starlette==2.1.3"

# Workaround for vLLM streaming tool_call bug (vllm#27641, fix in PR #35449 — not
# yet merged as of v0.14.1). When stream=true, large prompts route tool-call JSON
# into reasoning_content instead of the structured tool_calls array, so Hermes
# thinks the model is narrating instead of calling tools. Non-streaming works
# fine. Patch the one line in run_agent.py that toggles streaming.
RUN python3 - <<'PY'
path = '/opt/hermes/run_agent.py'
src = open(path, encoding='utf-8').read()
old = '_use_streaming = True\n                    # Provider signaled'
new = '_use_streaming = False  # patched: vLLM streaming tool_call bug workaround (vllm#27641)\n                    # Provider signaled'
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
