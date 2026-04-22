# CLAUDE.md

Заметки для будущего Claude (или нового разработчика). Цель — быстро сориентироваться, не перечитывая всю историю.

## Что это за проект

**Searcharvester 2.1.0** — self-hosted стек из трёх HTTP-сервисов и deep-research агента:

- `POST /search` — Tavily-совместимый поиск через SearXNG (100+ движков)
- `POST /extract` — URL → markdown через trafilatura, пресеты `s/m/l/f` + пагинация
- `POST /research` — deep research: спавнит эфемерный Hermes-контейнер, возвращает цитируемый markdown-отчёт

Примарно — английский README и документация (3 языка в `docs/`). Переписка со мной обычно на русском, но код-комментарии и доки англоязычные.

Pre-built образ в GHCR: `ghcr.io/vakovalskii/searcharvester:{latest,2.1.0}`.

## Устройство репозитория

| Путь | Что это |
|---|---|
| `docker-compose.yaml` | 4 always-on сервиса: `redis`, `searxng`, `docker-socket-proxy`, `tavily-adapter` (+ эфемерный `hermes-agent` спавнится на `/research`) |
| `config.yaml` | **gitignored**, создаётся из `config.example.yaml`; монтируется в SearXNG + адаптер |
| `simple_tavily_adapter/` | исходники адаптера (FastAPI + trafilatura + docker-py) |
| `simple_tavily_adapter/main.py` | роуты `/search`, `/extract`, `/research*`, `/health` |
| `simple_tavily_adapter/orchestrator.py` | лайфсайкл research-job: spawn / watch / timeout / cancel / cleanup |
| `simple_tavily_adapter/tests/` | unit + API тесты (19 штук), запекаются в образ |
| `hermes_skills/` | три наших skill'а в `agentskills.io` формате: search, extract, deep-research |
| `hermes-data/` | **gitignored**, volume для Hermes — туда синкаются наши skills |
| `jobs/` | **gitignored**, workspace каждой research-задачи (plan.md, notes.md, report.md, hermes.log) |
| `bench/` | SimpleQA-20 smoke-бенч и харнесс |
| `docs/{en,ru,zh}/` | документация на 3 языках + C4-диаграммы |
| `Caddyfile`, `.env`, `searxng-docker.service.template`, `searxng/limiter.toml` | остатки от upstream searxng-docker, compose их не читает |

## Архитектура `/research` (ядро новой версии)

Поток:

1. `POST /research {query}` → адаптер генерирует `job_id`, создаёт `jobs/{job_id}/`
2. Оркестратор через **docker-socket-proxy** шлёт `containers/create` в Docker daemon
3. Docker поднимает эфемерный `nousresearch/hermes-agent` контейнер:
   - volume `./hermes-data` → `/opt/data` (скиллы + конфиг Hermes)
   - volume `./jobs/{id}` → `/workspace` (куда агент пишет артефакты)
   - env: `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `SEARCHARVESTER_URL=http://host.docker.internal:8000`
   - CLI: `chat -Q -q "<query + mandatory contract suffix>" -s <skills> -t terminal --yolo --max-turns 30`
4. Оркестратор (asyncio task) ждёт `container.wait()` с таймаутом
5. По выходу читает логи, ищет маркер `REPORT_SAVED:`, если есть + `report.md` существует → `status=completed`
6. Контейнер удаляется (`container.remove(force=True)`), его ресурсы освобождаются

Ключевые моменты:

- **docker-socket-proxy** — whitelist'ит Docker API (`CONTAINERS=1 POST=1 IMAGES=1`, остальное 0). Адаптер ходит по `DOCKER_HOST=tcp://docker-socket-proxy:2375`, **не** касаясь `/var/run/docker.sock` напрямую. При компрометации адаптера получишь только create/start/kill/rm, не системный доступ.
- **Mandatory contract**: оркестратор добавляет к каждому query `MANDATORY_SUFFIX` — жёсткое требование «сохрани в `/workspace/report.md` + напечатай `REPORT_SAVED:`». Без него gpt-oss-120b для простых вопросов «срезает» методологию и отвечает прямо в stdout.
- **Host vs container paths**: в оркестраторе есть `jobs_dir` (внутри адаптера, для `mkdir` и чтения report.md) и `jobs_host_dir` (путь как видит Docker daemon на хосте, для bind-mount). Аналогично для hermes-data. **Не путать** — я уже один раз наступил на это.
- **LLM-агностик**: `provider: "custom"` в `hermes-data/config.yaml` + `OPENAI_BASE_URL` + `OPENAI_API_KEY`. Это единственный правильный путь для vLLM / локальных endpoint'ов — `provider: "vllm"` из доков упоминается, но на рантайме enum не принимает.

## Skills — три штуки

- `searcharvester-search/` — SKILL.md + `scripts/search.py`. Зовёт `/search` через `urllib`, возвращает компактный JSON без лишних полей.
- `searcharvester-extract/` — SKILL.md + `scripts/extract.py`. Зовёт `/extract` и `/extract/{id}/{page}`, отдаёт markdown + метаданные.
- `searcharvester-deep-research/` — **только SKILL.md, без кода**. Методология на ~200 строк: план → gather → gap-check → synthesise → verify. Скилл просто _описывает_ процесс для LLM; реальные tool-вызовы уже есть в первых двух скиллах.

Скиллы синкаются в `hermes-data/skills/` перед спавном. Формат переносимый ([agentskills.io](https://agentskills.io)) — те же скиллы работают в Claude Code, Cursor, OpenCode.

## Тесты

Написаны TDD-стилем (тесты раньше кода).

- `tests/test_orchestrator.py` — 12 unit-тестов с `FakeDockerClient` (fake Docker SDK). Покрывают: spawn, workspace mkdir, volume mounts, start failure, watch/logs parsing, timeout+kill, cancel, параллельные spawn'ы.
- `tests/test_research_api.py` — 7 FastAPI route-тестов с моком оркестратора. Покрывают: валидация query, статусы, отсутствие report на running, cancel.
- `tests/test_e2e.py` — интеграционный тест, gated через `RUN_E2E=1`. Реальный Hermes + vLLM.

Запуск быстро (всё запечено в образ):

```bash
docker compose exec tavily-adapter pytest -q        # unit + API, 19/19 за ~3с
RUN_E2E=1 pytest tests/test_e2e.py -v               # E2E, ~1-2 минуты
```

## Известные шероховатости

- **`score` результата `/search` — фейковый** (`0.9 - i*0.05`). Не настоящая релевантность.
- **`/extract` кеш — в памяти, TTL 30 мин.** После рестарта `tavily-adapter` старые `id` инвалидны → клиент должен повторить `POST /extract`. Осознанно, без SQLite/Redis ради простоты.
- **`tavily_client.py` отстал от `main.py`** — там BeautifulSoup-скрапинг и нет `/extract` логики. Либо синхронизируй, либо удали (HTTP API всё покрывает).
- **`hermes_skills/` vs `hermes-data/skills/`** — source в первом, mount во втором. При правке скилла **надо копировать** в `hermes-data/skills/` (или пересоздать volume). TODO: автоматический sync в оркестраторе при старте.
- **`Caddyfile` не подключён** к compose — если нужен HTTPS, добавь сервис вручную.
- **`limiter: false`** в `config.yaml` — SearXNG без анти-бот защиты. Ок для локалки, не ок для публичного endpoint'а.

## Git

Репо **не** является GitHub-форком (история чистая с `17906b8 Initial commit`). Раньше унаследовал коммиты от upstream `searxng-docker`, сейчас standalone. `master` удалена, default — `main`.

## Когда пишешь код / документацию

- Доки (README, docs/) — **английский primary**, RU + ZH переводы в `docs/{ru,zh}/`.
- CLAUDE.md и переписка — на русском.
- Не плоди новые markdown-файлы без нужды — проверь `docs/` сначала.
- Секреты (`secret_key`, `OPENAI_API_KEY`) только в `config.yaml` / `.env.hermes` (оба gitignored), **никогда** в коде.
- После изменений в `main.py` / `orchestrator.py` / `requirements.txt` — пересобрать образ: `docker compose build tavily-adapter && docker compose up -d`.
