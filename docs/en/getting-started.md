# Getting started

Assumes you have a working Docker (Docker Desktop, Colima, or native Docker Engine) and the `docker compose` command.

## 1. Clone the repo

```bash
git clone git@github.com:vakovalskii/searcharvester.git
cd searcharvester
```

## 2. Prepare the config

`config.yaml` isn't in the repo (it's `.gitignore`d). Create it from the template:

```bash
cp config.example.yaml config.yaml
```

Open `config.yaml` and change:

```yaml
server:
  secret_key: "YOUR_RANDOM_KEY_AT_LEAST_32_CHARACTERS"
```

Generate a key:

```bash
# any of the three
python3 -c "import secrets; print(secrets.token_hex(32))"
openssl rand -hex 32
head -c 32 /dev/urandom | xxd -p -c 32
```

Other settings (`adapter.searxng_url`, `adapter.scraper.*`, engine list) can stay at defaults.

## 3. Start the stack

```bash
docker compose up -d
```

First run: a couple of minutes (pull SearXNG + Valkey images, pull adapter from GHCR). Subsequent runs: a few seconds.

Check everything came up:

```bash
docker compose ps
```

You should see three services in `running` / `healthy`:
- `tavily-adapter` (healthcheck via `/health`)
- `searxng`
- `redis`

## 4. Verify it works

### SearXNG

Browser: [http://localhost:8999](http://localhost:8999) — classic SearXNG UI.

Via API:

```bash
curl "http://localhost:8999/search?q=test&format=json" | jq '.results | length'
```

### Tavily Adapter

```bash
# Search
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "bitcoin price", "max_results": 3}' | jq

# Extract page as markdown
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{"url":"https://en.wikipedia.org/wiki/Bitcoin","size":"s"}' | jq
```

Expected `/search` response shape:

```json
{
  "query": "bitcoin price",
  "results": [
    { "url": "...", "title": "...", "content": "...", "score": 0.9, "raw_content": null }
  ],
  "response_time": 1.23,
  "request_id": "..."
}
```

Health-check:

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"searxng-tavily-adapter","version":"2.0.0"}
```

Full endpoint list and parameters → [api.md](api.md).

## 5. Integrate into your code

### Option A. Official `tavily-python` client

```python
from tavily import TavilyClient

client = TavilyClient(
    api_key="anything",               # ignored by the adapter
    base_url="http://localhost:8000"  # ← your adapter
)
response = client.search(query="what is machine learning", max_results=5, include_raw_content=True)
```

### Option B. Local client (no HTTP)

When your code runs on the same host and you don't want HTTP overhead:

```python
from simple_tavily_adapter.tavily_client import TavilyClient

client = TavilyClient()  # reads config.yaml
response = client.search(query="...", max_results=5, include_raw_content=True)
```

### Option C. Raw HTTP

```python
import requests

r = requests.post("http://localhost:8000/search", json={
    "query": "...",
    "max_results": 5,
    "include_raw_content": True,
})
r.raise_for_status()
data = r.json()
```

## 6. Adapter dev without Docker

Hot-reload development:

```bash
# Keep SearXNG in Docker (or leave it as-is)
cd simple_tavily_adapter
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

While developing, point `config.yaml` at the published SearXNG port:

```yaml
adapter:
  searxng_url: "http://localhost:8999"   # instead of http://searxng:8080
```

Then run:

```bash
uvicorn main:app --reload --port 8000
```

> After you're done, **revert** `searxng_url` to `http://searxng:8080` — otherwise the Dockerised adapter can't find SearXNG (inside a container, `localhost` means the container itself).

## Next

- [api.md](api.md) — full request / response reference
- [operations.md](operations.md) — logs, restart, debugging, troubleshooting
- [architecture.md](architecture.md) — what's inside
