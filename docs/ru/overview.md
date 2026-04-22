# Обзор проекта

## Что это

**SearXNG Docker Tavily Adapter** — готовый Docker Compose стек, который даёт self-hosted замену API [Tavily](https://tavily.com). Вы запускаете стек, получаете HTTP-эндпойнт, полностью совместимый с Tavily, и можете подключать его к своему коду / LLM-пайплайнам без изменения логики.

Внутри стек состоит из трёх сервисов:

1. **SearXNG** — мета-поисковик, агрегирующий результаты из Google, DuckDuckGo, Brave и других.
2. **Tavily Adapter** — FastAPI-обёртка поверх SearXNG, расширенная trafilatura для извлечения main-content. Три эндпойнта:
   - `POST /search` — поиск + сниппеты (+ опц. markdown raw_content)
   - `POST /extract` — извлечение страницы в markdown с пресетами размера (s/m/l/f)
   - `GET /extract/{id}/{page}` — пагинация длинных документов
3. **Redis (Valkey)** — кеш SearXNG.

## Зачем это нужно

| Задача | Решение |
|---|---|
| Tavily стоит денег, есть лимиты | Адаптер бесплатный, лимитов нет — только лимиты самих поисковиков |
| Нужен контроль над источниками поиска | В `config.yaml` включаются / выключаются любые движки SearXNG |
| Запросы не должны уходить во внешний сервис | Всё крутится в локальной docker-сети |
| Нужен сырой текст страниц для LLM | Адаптер умеет опционально скрапить URL и отдавать текст в `raw_content` |

## Основные компоненты

### SearXNG
- Образ `docker.io/searxng/searxng:latest`
- Внутренний порт `8080`, публикуется на хосте как **`8999`**
- Настраивается тем же `config.yaml` (монтируется как `/etc/searxng/settings.yml`)
- Использует Valkey/Redis для кеша и сессий

### Tavily Adapter
- Собирается из `simple_tavily_adapter/` (Python 3.11, FastAPI, aiohttp, **trafilatura 2.x**)
- Публикует порт **`8000`**
- Четыре эндпойнта:
  - `POST /search` — Tavily-совместимый поиск + выбор движков/категорий
  - `POST /extract` — извлечение страницы в markdown с пресетами размера
  - `GET /extract/{id}/{page}` — пагинация длинных документов (TTL кеша 30 мин)
  - `GET /health` — health-check для Docker
- Читает `config.yaml` из `/srv/searxng-docker/config.yaml` (монтируется volume-ом)
- Скрапинг страниц выполняется параллельно через `asyncio.gather`
- Main-content extraction через **trafilatura** (readability++): убирает nav/footer/ads, выдаёт markdown с заголовками, списками, таблицами, ссылками

### Redis (Valkey)
- Образ `docker.io/valkey/valkey:8-alpine`
- На хост не публикуется, доступен только внутри docker-сети `searxng`
- Нужен SearXNG для кеширования и хранения состояния лимитера

## Как это стыкуется

```
┌──────────────┐   HTTP/JSON    ┌────────────────┐   HTTP/JSON    ┌──────────┐
│  Ваш код /   │───────────────▶│ Tavily Adapter │───────────────▶│ SearXNG  │
│  LLM / curl  │◀───────────────│   (порт 8000)  │◀───────────────│ (порт    │
└──────────────┘    Tavily      └────────┬───────┘   SearXNG      │  8999)   │
                   формат                │                         └────┬─────┘
                                         │ aiohttp + BeautifulSoup      │
                                         ▼                              ▼
                                  ┌──────────────┐              ┌──────────────┐
                                  │ Сайты-       │              │ Redis/Valkey │
                                  │ результаты   │              │   (кеш)      │
                                  │ (скрапинг)   │              └──────────────┘
                                  └──────────────┘
```

Конкретные C4-диаграммы смотри в [architecture.md](architecture.md).

## Что дальше

- Запустить с нуля → [getting-started.md](getting-started.md)
- Формат API → [api.md](api.md)
- Операционка (логи, отладка, траблшутинг) → [operations.md](operations.md)
