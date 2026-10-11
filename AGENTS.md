# AGENTS.md

Заметки для будущего агента (Codex, Devin) или нового разработчика. Цель — быстро сориентироваться, не перечитывая всю историю.

## Что это за проект

**Searcharvester 2.2.0** — self-hosted стек из HTTP-сервисов и deep-research агента:

- `POST /search` — Tavily-совместимый поиск через SearXNG (100+ движков)
- `POST /extract` — URL → markdown через trafilatura, пресеты `s/m/l/f` + пагинация
- `POST /research` — deep research: спавнит **subprocess `hermes acp`** в адаптере, стримит типизированные ACP-события в UI, возвращает цитируемый markdown-отчёт
- `GET /research/{id}/events` — SSE поток нормализованных событий агента (spawn / thought / tool_call / tool_result / done)

**v2.2 смена архитектуры:** было — эфемерный Docker-контейнер + stdout-regex. Стало — `FROM nousresearch/hermes-agent` + `hermes acp` subprocess + typed events через Python `acp` SDK. Нет больше docker-socket-proxy, нет docker-in-docker, нет regex-парсинга stdout.

Примарно — английский README и документация (3 языка в `docs/`). Переписка со мной обычно на русском, но код-комментарии и доки англоязычные.

Pre-built образ в GHCR: `ghcr.io/vakovalskii/searcharvester:{latest,2.1.0}`.

## Устройство репозитория

| Путь | Что это |
|---|---|
| `docker-compose.yaml` | 4 сервиса: `redis`, `searxng`, `tavily-adapter` (adapter + hermes в одном образе), `frontend` |
| `config.yaml` | **gitignored**, SearXNG settings; монтируется в SearXNG + адаптер |
| `simple_tavily_adapter/` | исходники адаптера (FastAPI + trafilatura + acp) |
| `simple_tavily_adapter/Dockerfile` | `FROM nousresearch/hermes-agent` — hermes CLI внутри образа адаптера |
| `simple_tavily_adapter/docker/entrypoint-adapter.sh` | Обёртка на entrypoint'ом hermes — поднимает HERMES_HOME + exec uvicorn |
| `simple_tavily_adapter/main.py` | FastAPI: `/search`, `/extract`, `/research*`, `/health` |
| `simple_tavily_adapter/orchestrator.py` | ACP-оркестратор: spawn `hermes acp` subprocess, connect_to_agent, forward session_update → Event |
| `simple_tavily_adapter/events.py` | Нормализатор ACP events → flat `Event{ts,job_id,agent_id,parent_id,type,payload}` |
| `simple_tavily_adapter/tests/` | `test_events.py` (normalizer) + `test_research_api.py` (routes, моки) — 12 unit-тестов |
| `hermes_skills/` | три наших skill'а в `agentskills.io` формате: search, extract, deep-research |
| `hermes-data/` | **gitignored**, HERMES_HOME: config.yaml, SOUL.md, skills/ |
| `jobs/` | **gitignored**, workspace каждой research-задачи (plan.md, report.md, hermes.log, extracts/) |
| `state/` | **gitignored**, состояние job'ов адаптера (STATE_DIR: job.json, events.jsonl, final_report.md) |
| `frontend/src/lib/api.ts` | `subscribeToJob()` → EventSource → типизированные `AgentEvent` |
| `frontend/src/components/LogTimeline.tsx` | Рендер timeline из типизированных событий (без regex) |
| `bench/` | SimpleQA-20 smoke-бенч и харнесс |
| `docs/{en,ru,zh}/` | документация на 3 языках + C4-диаграммы (⚠ отстали от 2.2) |

## Архитектура `/research` (v2.2 — ACP)

Поток:

1. `POST /research {query}` → адаптер генерирует `job_id`, создаёт `jobs/{job_id}/`
2. Оркестратор `asyncio.create_subprocess_exec("hermes", "acp", ...)` с `cwd=jobs/{id}` и env'ом из `.env.hermes` (OPENAI_API_KEY/BASE_URL, HERMES_HOME)
3. `acp.connect_to_agent(client, proc.stdin, proc.stdout)` → initialize → new_session → prompt
4. Каждый `session_update` callback-ом пролетает через `events.normalize_acp_update()` → нормализованный `Event` → append в `job.events` + `asyncio.Condition.notify_all()`
5. Клиент (UI или curl) подписан на `/research/{id}/events` (SSE) — `orchestrator.subscribe()` отдаёт replay + live stream через ту же Condition
6. `prompt` возвращается → читаем `report.md` → статус `completed` → эмитим финальный `done` event + `status` event → SSE закрывается

Ключевые моменты:

- **Один образ**: адаптер `FROM nousresearch/hermes-agent:latest` — hermes CLI лежит в PATH. `entrypoint-adapter.sh` повторяет логику upstream-ного entrypoint'а (HERMES_HOME bootstrap + gosu drop до user `hermes`) и затем `exec uvicorn`. Adapter и Hermes делят один контейнер → нет docker-in-docker, нет docker-socket-proxy.
- **ACP SDK**: `from acp import Client, connect_to_agent` — библиотека `agent-client-protocol` **уже запечена в hermes venv**, нам её не надо доустанавливать. Используем subclass `Client` с `async session_update()` callback'ом.
- **Flat Event schema** (`events.py`): `{ts, job_id, agent_id, parent_id, type, payload}`. `type` — union из `spawn|thought|message|tool_call|tool_result|plan|commands|note|done`. Транспорт-агностично — UI любого типа (React, curl -N, Python-клиент) потребляет одинаково. Если завтра уйдём с Hermes на Claude Code SDK, меняется только normalizer, не UI.
- **events.jsonl** пишется on-the-fly в `state/{id}/events.jsonl` (STATE_DIR адаптера) — post-mortem доступ без адаптера.
- **cwd = job workspace**, без `/workspace` bind'а. Агент пишет в `./report.md`. `MANDATORY_SUFFIX` в `orchestrator.py` это говорит; SKILL.md тоже переведён на относительные пути.
- **LLM-агностик**: `provider: "custom"` в `hermes-data/config.yaml` + `OPENAI_BASE_URL` + `OPENAI_API_KEY` (из `.env.hermes` рядом с docker-compose.yaml — сервисный `env_file`, gitignored, шаблон `.env.hermes.example`). `provider: "vllm"` из доков на рантайме не принимается — используем "custom".

## Skills — три штуки

- `searcharvester-search/` — SKILL.md + `scripts/search.py`. Зовёт `/search` через `urllib`, возвращает компактный JSON без лишних полей.
- `searcharvester-extract/` — SKILL.md + `scripts/extract.py` + `scripts/fragments.py`. Зовёт `/extract`, сохраняет полный markdown в `./extracts/<id>.md`, отдаёт указатель + превью. С `--query "<подвопрос>"` сверху режет сохранённый файл на структурные чанки и BM25-ранжирует их по запросу: агент получает лучшие фрагменты с путём заголовков и номерами строк, эскалация — соседние чанки / `sed -n 'a,bp'` / весь файл (он уже на диске). `fragments.py` работает и отдельно (`--file --query`) — переранжировать сохранённый файл под новый вопрос без повторного обращения к сети.
- `searcharvester-deep-research/` — **только SKILL.md, без кода**. Методология на ~200 строк: план → gather → gap-check → synthesise → verify. Скилл просто _описывает_ процесс для LLM; реальные tool-вызовы уже есть в первых двух скиллах.

Скиллы синкаются в `hermes-data/skills/` перед спавном. Формат переносимый ([agentskills.io](https://agentskills.io)) — те же скиллы работают в Claude Code, Cursor, OpenCode.

## Тесты

- `tests/test_events.py` — 5 unit-тестов нормализатора ACP → Event. Не требуют Docker/hermes.
- `tests/test_research_api.py` — 7 FastAPI route-тестов с моком оркестратора (AsyncMock).
- `tests/test_e2e.py` — gated `RUN_E2E=1`. Реальный Hermes + vLLM end-to-end.

Старые `test_orchestrator.py` (FakeDockerClient) удалены — оркестратор больше не использует docker-py. Реальное покрытие "ACP subprocess → events" сейчас даёт E2E (или Playwright smoke через UI).

```bash
docker compose exec tavily-adapter /opt/hermes/.venv/bin/python -m pytest -q  # 12 passed за ~2с
# (pytest не в PATH как алиас — идёт через venv-python)
```

## Известные шероховатости

- **`score` результата `/search` — фейковый** (`0.9 - i*0.05`). Не настоящая релевантность.
- **`/extract` кеш — в памяти, TTL 30 мин.** После рестарта `tavily-adapter` старые `id` инвалидны → клиент должен повторить `POST /extract`.
- **Sub-agent events через ACP не видны**: `delegate_task` — это tool_call лида, но внутренности сабов (их search/extract) в parent ACP stream не прилетают. TODO: тейлить `~/.hermes/sessions/session_*.json` для каждого спавнутого саба и мёрджить в общий поток (через `parent_id`).
- **`tavily_client.py` отстал от `main.py`** — там BeautifulSoup-скрапинг и нет `/extract` логики. Либо синхронизируй, либо удали.
- **`hermes_skills/` vs `hermes-data/skills/`** — source в первом, mount во втором. При правке скилла **надо копировать** в `hermes-data/skills/`. TODO: автоматический sync в entrypoint.
- **Изоляция agent ↔ adapter**: теперь в одном контейнере. Hermes `--yolo` terminal-команды выполняются в адаптерном user space. По сравнению с v2.1 (отдельный контейнер) — даунгрейд изоляции. Критично если кто-то скормит промпт с `rm -rf /app/…`. Пока ок для dev.
- **`Caddyfile` не подключён** к compose — если нужен HTTPS, добавь сервис вручную.
- **`limiter: false`** в `config.yaml` — SearXNG без анти-бот защиты. Ок для локалки, не ок для публичного endpoint'а.

## Git

Репо **не** является GitHub-форком (история чистая с `17906b8 Initial commit`). Раньше унаследовал коммиты от upstream `searxng-docker`, сейчас standalone. `master` удалена, default — `main`.

## Когда пишешь код / документацию

- Доки (README, docs/) — **английский primary**, RU + ZH переводы в `docs/{ru,zh}/`.
- AGENTS.md и переписка — на русском.
- Не плоди новые markdown-файлы без нужды — проверь `docs/` сначала.
- Секреты (`secret_key`, `OPENAI_API_KEY`) только в `config.yaml` / `.env.hermes` (оба gitignored), **никогда** в коде.
- После изменений в `main.py` / `orchestrator.py` / `events.py` / `Dockerfile` / `entrypoint-adapter.sh` — пересобрать образ: `docker compose up -d --build tavily-adapter`.
- **`.env.hermes` обязателен, но флаги не нужны**: сервисный `env_file` подхватывает его автоматически, хватает `docker compose up -d`. Файл лежит рядом с docker-compose.yaml (gitignored, шаблон `.env.hermes.example`); без него `OPENAI_API_KEY`/`OPENAI_BASE_URL` попадают в контейнер пустыми и `hermes acp` падает на первом же LLM-вызове.
- LLM event schema — **контракт с UI**. Если добавляешь новый `type` в `events.py`, обнови `AgentEventType` в `frontend/src/lib/api.ts` + `renderEvent` в `LogTimeline.tsx`.
