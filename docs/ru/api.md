# API адаптера

Базовый URL: `http://localhost:8000` (или тот, куда вы пробросили порт `8000` контейнера `tavily-adapter`).

Авторизации нет. Любой `api_key` в запросе игнорируется — он сохранён только для совместимости с клиентами Tavily.

Эндпойнты:

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/search` | Поиск: ссылки + сниппеты (+ опц. markdown raw_content) |
| `POST` | `/extract` | Извлечение страницы в markdown (s/m/l/f + пагинация) |
| `GET` | `/extract/{id}/{page}` | Следующая страница извлечённого контента (только для `size=f`) |
| `GET` | `/health` | Health-check |

---

## `POST /search`

Tavily-совместимый поиск. Принимает JSON, возвращает JSON.

### Запрос

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| `query` | string | — | обязательное, текст поискового запроса |
| `max_results` | int | `10` | сколько результатов вернуть |
| `include_raw_content` | bool | `false` | `true` → скачать страницы и положить markdown в `raw_content` (обрезан до `adapter.scraper.max_content_length`) |
| `engines` | string \| null | `"google,duckduckgo,brave"` | comma-separated список движков SearXNG. Примеры: `"google"`, `"duckduckgo,brave"`, `"yandex,mojeek"` |
| `categories` | string \| null | `"general"` | категория SearXNG: `general`, `news`, `images`, `videos`, `map`, `music`, `it`, `science`, `files`, `social` |

Пример:

```bash
curl -X POST http://localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "последние новости об AI",
    "max_results": 5,
    "engines": "duckduckgo,brave",
    "categories": "news"
  }'
```

### Ответ

Схема Tavily (не менялась ради обратной совместимости):

```json
{
  "query": "последние новости об AI",
  "follow_up_questions": null,
  "answer": null,
  "images": [],
  "results": [
    {
      "url": "https://example.com/article",
      "title": "AI Breakthrough 2026",
      "content": "Краткий snippet от поисковика...",
      "score": 0.9,
      "raw_content": "# AI Breakthrough 2026\n\nМаркдаун контент..."
    }
  ],
  "response_time": 1.42,
  "request_id": "uuid"
}
```

**`raw_content`** теперь возвращается в **markdown** (через trafilatura) — с заголовками, списками, ссылками. Раньше был plain-text.

### Коды ошибок

| Код | Когда |
|---|---|
| `200` | ок (даже если `results` пустой) |
| `422` | невалидный JSON / нет `query` |
| `500` | SearXNG упал |
| `504` | SearXNG не ответил за 30 секунд |

---

## `POST /extract`

Скачивает страницу, извлекает main-content (trafilatura), возвращает markdown.

### Запрос

| Поле | Тип | Дефолт | Описание |
|---|---|---|---|
| `url` | string | — | обязательное, URL страницы |
| `size` | `"s"` \| `"m"` \| `"l"` \| `"f"` | `"m"` | пресет размера (см. ниже) |

**Пресеты размера:**

| Значение | Поведение | Когда использовать |
|---|---|---|
| `s` | обрезать до **5 000** символов | короткая выжимка для LLM с маленьким контекстом |
| `m` | обрезать до **10 000** символов | обычный размер для агентного чтения |
| `l` | обрезать до **25 000** символов | глубокое чтение статьи |
| `f` | **полный** контент, разбитый по страницам по 25 000 символов | когда нужен весь документ |

### Ответ (size ∈ s/m/l)

```json
{
  "id": "b275618ca10e6c62",
  "url": "https://en.wikipedia.org/wiki/Docker_(software)",
  "title": "Docker (software) - Wikipedia",
  "format": "md",
  "size": "m",
  "content": "# Docker (software)\n\n...",
  "chars": 10000,
  "total_chars": 33430,
  "pages": { "current": 1, "total": 1, "page_size": 10000 }
}
```

- `chars` — длина `content` в этом ответе.
- `total_chars` — полная длина извлечённого контента **до обрезки** (полезно, чтобы понять, не потерял ли контекст).
- Для `s/m/l` пагинации нет — `pages.total: 1`.

### Ответ (size = f)

Если контент помещается в одну страницу (≤ 25 000 символов):

```json
{
  "id": "b275618ca10e6c62",
  "url": "...",
  "title": "...",
  "format": "md",
  "size": "f",
  "content": "...",
  "chars": 18500,
  "total_chars": 18500,
  "pages": { "current": 1, "total": 1, "page_size": 25000 }
}
```

Если контент длиннее 25 000 символов:

```json
{
  "id": "b275618ca10e6c62",
  "url": "...",
  "title": "...",
  "format": "md",
  "size": "f",
  "content": "первые 25000 символов markdown...",
  "chars": 25000,
  "total_chars": 67000,
  "pages": {
    "current": 1,
    "total": 3,
    "page_size": 25000,
    "next": "/extract/b275618ca10e6c62/2"
  }
}
```

Поле `pages.next` содержит готовый путь для `GET`-запроса следующей страницы.

### Коды ошибок

| Код | Когда |
|---|---|
| `200` | ок |
| `422` | страница не содержит извлекаемого контента (trafilatura вернул пусто) |
| `502` | не удалось скачать URL (не-200 ответ / сетевая ошибка) |

---

## `GET /extract/{id}/{page}`

Возвращает страницу ранее извлечённого контента. Работает только для документов, запрошенных с `size=f`.

### Параметры пути

| Параметр | Тип | Описание |
|---|---|---|
| `id` | string (16 hex) | `id` из предыдущего `POST /extract` |
| `page` | int ≥ 1 | номер страницы |

### Пример

```bash
curl http://localhost:8000/extract/b275618ca10e6c62/2
```

Ответ — та же структура, что у `POST /extract` с `size=f`, но с `pages.current = page`.

### Кеш и TTL

- Сервер хранит извлечённый контент в **in-memory кеше на 30 минут** (keyed by `id = md5(url)[:16]`).
- После рестарта контейнера кеш очищается → нужен повторный `POST /extract`.
- При просроченном / неизвестном `id` → `404`:
  ```json
  {"detail": "id не найден или просрочен (TTL 30 мин). Повторите POST /extract."}
  ```

### Коды ошибок

| Код | Когда |
|---|---|
| `200` | ок |
| `404` | `id` не найден (или `page` больше `total`) |
| `422` | некорректные параметры пути |

---

## `GET /health`

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"searxng-tavily-adapter","version":"2.0.0"}
```

Docker-compose использует этот эндпойнт как healthcheck каждые 30 секунд.

---

## Поведение и гарантии

- **`/search` score — фейковый** (`0.9 - i*0.05`). Не используйте для ранжирования.
- **`/extract` → markdown через trafilatura**: сохраняются заголовки, списки, таблицы, ссылки. Navigation/header/footer/ads вырезаются автоматически.
- **Таймауты:**
  - `/search` → SearXNG: 30 c (зашит в коде)
  - `/extract` → скрейпинг URL: `adapter.scraper.timeout` из `config.yaml` (по умолчанию 10 c)
- **Параллельность `/search` со скрейпингом** (при `include_raw_content=true`) — все URL качаются через `asyncio.gather` параллельно.
- **`/extract` НЕ является batch-эндпойнтом** — принимает один URL. Для нескольких URL делайте несколько запросов.

---

## Агентные паттерны использования

### 1. Быстрый поиск + снипеты

```bash
curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"что такое RAG","max_results":5}' \
  | jq '.results[] | {title, url, content}'
```

### 2. Поиск + быстрое чтение первой ссылки

```bash
URL=$(curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"architecture decision records","max_results":1}' \
  | jq -r '.results[0].url')

curl -sX POST localhost:8000/extract -H 'Content-Type: application/json' \
  -d "{\"url\":\"$URL\",\"size\":\"m\"}" \
  | jq -r '.content'
```

### 3. Глубокое чтение длинной статьи (пагинация)

```bash
# Первая страница
RESP=$(curl -sX POST localhost:8000/extract -H 'Content-Type: application/json' \
  -d '{"url":"https://en.wikipedia.org/wiki/Linux","size":"f"}')

echo "$RESP" | jq -r '.content'
ID=$(echo "$RESP" | jq -r '.id')
TOTAL=$(echo "$RESP" | jq -r '.pages.total')

# Остальные страницы
for P in $(seq 2 $TOTAL); do
  curl -s localhost:8000/extract/$ID/$P | jq -r '.content'
done
```

### 4. Поиск news + чтение лучшего результата

```bash
curl -sX POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"query":"GPT-5 release","max_results":3,"categories":"news","engines":"duckduckgo,brave"}'
```
