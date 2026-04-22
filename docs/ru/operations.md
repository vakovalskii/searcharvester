# Операционка

Что делать со стеком после того, как он запущен. Команды ниже подразумевают, что вы в корне репозитория.

## Повседневные команды

| Действие | Команда |
|---|---|
| Поднять стек в фоне | `docker compose up -d` |
| Остановить | `docker compose stop` |
| Остановить и удалить контейнеры | `docker compose down` |
| Удалить контейнеры + volumes (снести кеш) | `docker compose down -v` |
| Пересобрать адаптер после правок | `docker compose build tavily-adapter && docker compose up -d` |
| Посмотреть статус | `docker compose ps` |
| Логи всех сервисов (follow) | `docker compose logs -f` |
| Логи одного сервиса | `docker compose logs -f tavily-adapter` |
| Рестартнуть один сервис | `docker compose restart tavily-adapter` |
| Зайти в контейнер | `docker compose exec tavily-adapter sh` |

После правок `config.yaml` оба сервиса надо рестартнуть, чтобы они перечитали файл:

```bash
docker compose restart searxng tavily-adapter
```

## Проверка здоровья

```bash
# Адаптер
curl -sf http://localhost:8000/health && echo OK

# SearXNG
curl -sf "http://localhost:8999/search?q=ping&format=json" | jq '.results | length'

# Docker-level healthcheck
docker inspect --format='{{.State.Health.Status}}' tavily-adapter
```

## Смоук-тест

Внутри адаптера лежит `simple_tavily_adapter/test_client.py`:

```bash
docker compose exec tavily-adapter python test_client.py
```

Или с хоста (если у вас установлены requirements локально):

```bash
cd simple_tavily_adapter && python test_client.py
```

## Логи и отладка

Логи адаптера включают `request_id`, время ответа и количество результатов:

```
INFO:main:Search request: цена bitcoin
INFO:main:Search completed: 3 results in 1.42s
```

Если что-то идёт не так:

1. **Пустой `results[]`** чаще всего означает, что SearXNG не смог добраться до движков. Проверьте напрямую:
   ```bash
   docker compose exec searxng wget -qO- "http://localhost:8080/search?q=test&format=json" | head -c 500
   ```
2. **504 Gateway Timeout** от адаптера → SearXNG тупит > 30 с. Посмотрите его логи (`docker compose logs searxng`), может быть заблокирован конкретный движок. Отключите его в `config.yaml` → `engines:` → `disabled: true`.
3. **500 Internal Server Error** → лог адаптера: `docker compose logs tavily-adapter | tail -50`.
4. **`raw_content: null` везде** → целевые сайты блокируют User-Agent адаптера или таймаут слишком маленький. Поправьте:
   ```yaml
   adapter:
     scraper:
       timeout: 20
       user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ..."
   ```

## Типовые проблемы

### `/extract` возвращает 404 на `{id}/{page}` после рестарта

In-memory кеш `/extract` живёт в процессе `tavily-adapter` и сбрасывается при его рестарте или после 30 минут простоя. После `docker compose restart tavily-adapter` все старые `id` инвалидны — клиент должен повторить `POST /extract`. Это ожидаемое поведение.

Если это мешает — см. CLAUDE.md, вариант с персистентным кешем (SQLite / Redis) не реализован сознательно ради простоты.

### «Forbidden» / капча от Google

SearXNG ходит в Google без cookies и лимитирован по IP. Если вы из одной сетки шлёте много запросов, Google начнёт капчить. Варианты:

- Отключить Google (`engines: - name: google, disabled: true`), оставить DuckDuckGo + Brave.
- Включить другие движки (`yandex`, `mojeek`, и т.п. — см. [документацию SearXNG](https://docs.searxng.org/)).
- Пустить SearXNG через прокси (настраивается в его `settings.yml`).

### Порт 8000 или 8999 занят

Поменяйте маппинг в `docker-compose.yaml`:

```yaml
tavily-adapter:
  ports:
    - "8010:8000"    # хост:контейнер
searxng:
  ports:
    - "0.0.0.0:9000:8080"
```

Не забудьте обновить и `adapter.searxng_url`, если меняли внутренний порт.

### Адаптер не видит SearXNG

В docker-compose имя сервиса — это имя хоста внутри docker-сети. Значение по умолчанию:

```yaml
adapter:
  searxng_url: "http://searxng:8080"
```

Если вы запускаете адаптер локально (не в docker), используйте `http://localhost:8999` — публикуемый хост-порт SearXNG.

### После `git pull` адаптер не обновился

Образ адаптера собирается локально. Docker Compose его не пересоберёт сам:

```bash
docker compose build tavily-adapter
docker compose up -d
```

Или одной командой: `docker compose up -d --build`.

### Забыл скопировать `config.yaml`

SearXNG стартует и сразу падает с ошибкой про `settings.yml`, адаптер работает на fallback-дефолтах из `config_loader.py` (без `searxng_url` не работает). Решение:

```bash
cp config.example.yaml config.yaml
# поправьте secret_key
docker compose restart
```

## Обновление образов

```bash
docker compose pull searxng redis           # pull обновлений
docker compose build tavily-adapter         # если правили адаптер
docker compose up -d
```

При смене мажорной версии SearXNG читайте её release notes — могут поменяться поля в `settings.yml`.

## Продакшен-чеклист (если вдруг)

Если собираетесь выставить стек наружу:

- [ ] Включить `limiter: true` в `config.yaml` и настроить `searxng/limiter.toml`.
- [ ] Поставить Caddy / nginx перед адаптером и SearXNG с TLS (в репе лежит `Caddyfile`, но он **не подключён** к `docker-compose.yaml` — добавить сервис вручную).
- [ ] Добавить auth (Basic Auth через Caddy, или вынести адаптер за JWT-gateway).
- [ ] Ограничить SearXNG снаружи — публиковать только адаптер.
- [ ] Задать `SEARXNG_BASE_URL` с реальным доменом в `docker-compose.yaml`.
- [ ] Использовать `.env` для секретов, не коммитить `config.yaml`.
- [ ] Настроить ротацию логов (сейчас `max-size: 1m, max-file: 1` — нормально для dev, мало для прод).

## Архивы / backup

Ничего ценного в volumes не хранится — только кеш SearXNG и состояние Valkey. Можно спокойно делать `docker compose down -v` без потери данных.

Исключение — ваш `config.yaml`. Храните его в менеджере секретов / приватной репе, если используете уникальные настройки или `secret_key`.
