# CLAUDE.md

Эти заметки нужны, чтобы будущий Claude (или новый разработчик) быстро ориентировался в репозитории.

## Что это за проект

**Searcharvester** — Docker Compose стек, который поднимает **SearXNG** (мета-поисковик) и **FastAPI-адаптер** с trafilatura для извлечения markdown. API частично совместим с [Tavily](https://tavily.com) (через `/search`), плюс собственный `/extract` с пресетами размера и пагинацией. Smысл: drop-in замена платного Tavily + инструмент для AI-агентов «собрать урожай с интернета». Подробности в `README.md` и `docs/`.

Образ опубликован в GHCR: `ghcr.io/vakovalskii/searcharvester:latest`. В `docker-compose.yaml` по умолчанию используется `image:`, но секция `build:` сохранена — `docker compose up --build` пересобирает локально.

## Устройство репозитория

| Путь | Что это |
|---|---|
| `docker-compose.yaml` | оркестрация: `redis` (valkey), `searxng`, `tavily-adapter` |
| `config.example.yaml` | шаблон единого конфига (и SearXNG, и адаптера) |
| `config.yaml` | **gitignored**, создаётся из шаблона; монтируется в оба сервиса |
| `simple_tavily_adapter/` | исходники адаптера (FastAPI + aiohttp + BeautifulSoup) |
| `searxng/limiter.toml` | настройки лимитера SearXNG (сейчас не монтируется в compose — артефакт) |
| `Caddyfile` | конфиг Caddy для HTTPS-фронта; **не подключён** к compose, остался от upstream |
| `.env` | placeholders для `SEARXNG_HOSTNAME` / `LETSENCRYPT_EMAIL`, текущим compose не читаются |
| `searxng-docker.service.template` | systemd unit для Linux-хоста |
| `docs/` | подробная документация + C4-диаграммы |

## Адаптер в двух словах

- `simple_tavily_adapter/main.py` — FastAPI, эндпойнты:
  - `POST /search` — Tavily-совместимый поиск + опциональные `engines`, `categories`, markdown `raw_content` через trafilatura
  - `POST /extract` — извлечение страницы в markdown через trafilatura. Пресеты `size`: `s` (5k), `m` (10k), `l` (25k), `f` (полный с пагинацией по 25k)
  - `GET /extract/{id}/{page}` — следующие страницы (для `size=f`). In-memory кеш с TTL 30 мин, keyed by `md5(url)[:16]`
  - `GET /health`
- `simple_tavily_adapter/tavily_client.py` — Python-класс `TavilyClient` для in-process использования. **Пока не знает про trafilatura и новые поля** — отстаёт от `main.py`, см. «шероховатости».
- `simple_tavily_adapter/config_loader.py` — читает `config.yaml` по пути `/srv/searxng-docker/config.yaml` (путь монтирования в контейнере). Есть fallback на дефолты если файла нет.
- Поток `/search`: клиент → POST к SearXNG `/search?format=json&engines=...&categories=...` → (опц.) параллельный fetch URL-ов через aiohttp → trafilatura → ответ в формате Tavily.
- Поток `/extract`: клиент → fetch URL → trafilatura.extract(output_format='markdown') → кеш → нарезка по size → ответ.

## Как запускать

```bash
cp config.example.yaml config.yaml        # один раз, перед первым стартом
# отредактируй server.secret_key (>= 32 символа)
docker compose up -d
curl -X POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"test","max_results":3}'
```

Порты на хосте: **8000** — адаптер, **8999** — SearXNG UI/API. Redis только во внутренней docker-сети.

Локальная разработка адаптера без Docker:

```bash
cd simple_tavily_adapter
pip install -r requirements.txt
# SearXNG должен быть доступен; для локала поправь adapter.searxng_url на http://localhost:8999
python main.py
```

## Известные шероховатости (держи в голове при правках)

- **`/search`: engines и categories теперь приходят из запроса**, но `language`, `safesearch`, `pageno` всё ещё захардкожены в `main.py`. Поля `adapter.search.*` в конфиге по-прежнему не читаются основной функцией поиска — это легаси config_loader.
- **`tavily_client.py` отстал от `main.py`.** Там до сих пор BeautifulSoup-скрапинг и нет `/extract`-логики. Если нужно — синхронизируй (или просто удали client.py, HTTP-API покрывает всё).
- **`score` результата — фейковый**: `0.9 - i*0.05`. Не настоящая релевантность.
- **Кеш `/extract` — в памяти, TTL 30 мин.** После рестарта `tavily-adapter` — `id` инвалидны, клиент должен повторить `POST /extract`. Осознанный выбор: не хочется персистентности ради простоты.
- **`config.yaml` монтируется дважды** (в `searxng` как `/etc/searxng/settings.yml`, в `tavily-adapter` как `/srv/searxng-docker/config.yaml`). Один файл — две точки монтирования.
- **`Caddyfile` не подключён** к `docker-compose.yaml`. Если нужен HTTPS — добавлять сервис Caddy вручную.
- **`limiter: false`** в конфиге — SearXNG без анти-бот защиты. Ок для локалки, не ок для публичного доступа.
- **`version: "3.7"`** в compose — устаревшее поле, Docker Compose v2 кидает warning, но игнорирует.
- **trafilatura 2.x** установлена, тянет lxml + justext + dateparser — образ вырос на ~50MB. Это нормально.

## Git / апстрим

Репозиторий форкнут от [searxng/searxng-docker](https://github.com/searxng/searxng-docker) — отсюда `Caddyfile`, `.env`, `searxng-docker.service.template`, `searxng/limiter.toml`. Адаптер и `config.example.yaml` — добавлены локально (см. коммит `5af08af feat: Add SearXNG Tavily Adapter`).

## Когда пишешь код / документацию

- Проект и вся документация на русском — пиши на русском, если не попросили иначе.
- Не плоди новые markdown-файлы без нужды — проверь `docs/` и `README.md`.
- Секреты (`secret_key`, токены) только в `config.yaml` или `.env`, никогда в коде.
