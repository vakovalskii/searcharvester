# Operations

What to do with the stack once it's up. Commands below assume you're in the repo root.

## Everyday commands

| Action | Command |
|---|---|
| Start stack in the background | `docker compose up -d` |
| Stop | `docker compose stop` |
| Stop and remove containers | `docker compose down` |
| Remove containers + volumes (wipe cache) | `docker compose down -v` |
| Pull latest adapter image from GHCR | `docker compose pull tavily-adapter && docker compose up -d` |
| Force-rebuild adapter locally | `docker compose build tavily-adapter && docker compose up -d` |
| Status | `docker compose ps` |
| Follow logs for all services | `docker compose logs -f` |
| Follow logs for one service | `docker compose logs -f tavily-adapter` |
| Restart one service | `docker compose restart tavily-adapter` |
| Shell into a container | `docker compose exec tavily-adapter sh` |

After changes to `config.yaml`, restart both services so they re-read the file:

```bash
docker compose restart searxng tavily-adapter
```

## Health checks

```bash
# Adapter
curl -sf http://localhost:8000/health && echo OK

# SearXNG
curl -sf "http://localhost:8999/search?q=ping&format=json" | jq '.results | length'

# Docker-level healthcheck
docker inspect --format='{{.State.Health.Status}}' tavily-adapter
```

## Smoke test

There's a `simple_tavily_adapter/test_client.py` inside the adapter:

```bash
docker compose exec tavily-adapter python test_client.py
```

Or from the host (if you have requirements installed locally):

```bash
cd simple_tavily_adapter && python test_client.py
```

## Logs and debugging

Adapter logs include `request_id`, response time, and result count:

```
INFO:main:Search request: bitcoin price
INFO:main:Search completed: 3 results in 1.42s
```

When something goes wrong:

1. **Empty `results[]`** most often means SearXNG couldn't reach the engines. Check directly:
   ```bash
   docker compose exec searxng wget -qO- "http://localhost:8080/search?q=test&format=json" | head -c 500
   ```
2. **504 Gateway Timeout** from the adapter → SearXNG is stuck > 30s. Check its logs (`docker compose logs searxng`) — a specific engine might be blocked. Disable it in `config.yaml` → `engines:` → `disabled: true`.
3. **500 Internal Server Error** → adapter log: `docker compose logs tavily-adapter | tail -50`.
4. **`raw_content: null` everywhere** → target sites are blocking the adapter's User-Agent or the timeout is too small. Fix:
   ```yaml
   adapter:
     scraper:
       timeout: 20
       user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ..."
   ```

## Common issues

### `/extract` returns 404 on `{id}/{page}` after a restart

The `/extract` in-memory cache lives in the `tavily-adapter` process and is wiped on restart or after 30 minutes of inactivity. After `docker compose restart tavily-adapter` all old `id`s are invalid — the client must repeat `POST /extract`. This is expected.

If this bothers you — see CLAUDE.md. A persistent cache (SQLite / Redis) is intentionally not implemented to keep things simple.

### "Forbidden" / CAPTCHA from Google

SearXNG queries Google without cookies and is IP-rate-limited. If you send many queries from one IP, Google will start CAPTCHA-ing. Options:

- Disable Google (`engines: - name: google, disabled: true`), keep DuckDuckGo + Brave.
- Enable other engines (`yandex`, `mojeek`, etc. — see the [SearXNG docs](https://docs.searxng.org/)).
- Route SearXNG through a proxy (configured in its `settings.yml`).

### Port 8000 or 8999 is taken

Change the mapping in `docker-compose.yaml`:

```yaml
tavily-adapter:
  ports:
    - "8010:8000"    # host:container
searxng:
  ports:
    - "0.0.0.0:9000:8080"
```

Don't forget to update `adapter.searxng_url` if you changed the internal port.

### Adapter can't see SearXNG

In docker-compose, the service name is the hostname inside the docker network. Default:

```yaml
adapter:
  searxng_url: "http://searxng:8080"
```

If you run the adapter locally (not in docker), use `http://localhost:8999` — SearXNG's published host port.

### Adapter didn't update after `git pull`

If you had a local build, Docker Compose won't rebuild automatically:

```bash
docker compose build tavily-adapter
docker compose up -d
```

Or in one command: `docker compose up -d --build`.

If you're using the pre-built image from GHCR:

```bash
docker compose pull tavily-adapter
docker compose up -d
```

### Forgot to copy `config.yaml`

SearXNG starts and immediately crashes with an error about `settings.yml`; the adapter falls back to defaults from `config_loader.py` (but without `searxng_url` it won't work). Fix:

```bash
cp config.example.yaml config.yaml
# edit secret_key
docker compose restart
```

## Updating images

```bash
docker compose pull                          # pull updates for all images
docker compose up -d
```

When a major SearXNG version changes, read its release notes — `settings.yml` fields may change.

## Production checklist (just in case)

If you're going to expose the stack publicly:

- [ ] Set `limiter: true` in `config.yaml` and configure `searxng/limiter.toml`.
- [ ] Put Caddy / nginx in front of the adapter and SearXNG with TLS (the repo has a `Caddyfile` but it's **not wired** into `docker-compose.yaml` — add a service manually).
- [ ] Add auth (Basic Auth via Caddy, or put the adapter behind a JWT gateway).
- [ ] Restrict SearXNG externally — publish only the adapter.
- [ ] Set `SEARXNG_BASE_URL` to your real domain in `docker-compose.yaml`.
- [ ] Use `.env` for secrets, don't commit `config.yaml`.
- [ ] Configure log rotation (currently `max-size: 1m, max-file: 1` — fine for dev, tiny for prod).

## Backups

Nothing valuable lives in volumes — just SearXNG cache and Valkey state. Safe to `docker compose down -v` without data loss.

Exception — your `config.yaml`. Store it in a secrets manager / private repo if you use unique settings or a critical `secret_key`.
