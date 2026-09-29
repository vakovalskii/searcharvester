# Pinned: `latest` moved to v0.21 (2026-09-24) and the old streaming patch anchor
# is gone. Bump deliberately and re-run bench/ after every bump.
ARG HERMES_VERSION=v2026.9.24
FROM nousresearch/hermes-agent:${HERMES_VERSION}

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

RUN uv pip install --no-cache-dir --python /opt/hermes/.venv/bin/python3 \
    "trafilatura>=1.12,<3.0" \
    "lxml>=4.9" \
    "beautifulsoup4==4.12.2" \
    "sse-starlette==2.1.3" \
    "pytest==8.3.3" \
    "pytest-asyncio==0.24.0"

# Hermes patches (streaming toggle for the vLLM tool_call bug, see the script).
# A plain script instead of a heredoc RUN so the image builds without BuildKit too.
COPY docker/patch_hermes.py /tmp/patch_hermes.py
RUN python3 /tmp/patch_hermes.py

# tirith scans every terminal command before it runs (useful: the agent has a
# shell). Hermes downloads it on first use, which from some networks times out
# and costs ~17s per terminal session. Install it at build time instead; the
# installer verifies the SHA-256. Lookup order is PATH first, so /usr/local/bin wins.
RUN cd /opt/hermes && HERMES_HOME=/tmp/tirith-home /opt/hermes/.venv/bin/python -c \
      "from tools.tirith_security import _install_tirith; print(_install_tirith())" \
 && install -m 0755 /tmp/tirith-home/bin/tirith /usr/local/bin/tirith \
 && tirith --version

RUN mkdir -p /app && chown hermes:hermes /app
COPY --chown=hermes:hermes . /app/

COPY --chown=root:root docker/entrypoint-adapter.sh /entrypoint-adapter.sh
RUN chmod +x /entrypoint-adapter.sh

WORKDIR /app
EXPOSE 8000

ENTRYPOINT ["/entrypoint-adapter.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
