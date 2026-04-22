# Запуск с нуля

Предполагается, что у вас уже есть работающий Docker (Docker Desktop, Colima или нативный Docker Engine) и команда `docker compose`.

## 1. Клонировать репозиторий

```bash
git clone git@github.com:vakovalskii/searcharvester.git
cd searcharvester
```

## 2. Подготовить конфиг

`config.yaml` в репе нет (он в `.gitignore`), его надо создать из шаблона:

```bash
cp config.example.yaml config.yaml
```

Откройте `config.yaml` и обязательно поменяйте:

```yaml
server:
  secret_key: "ВАШ_СЛУЧАЙНЫЙ_КЛЮЧ_МИНИМУМ_32_СИМВОЛА"
```

Сгенерировать ключ:

```bash
# любой из трёх вариантов
python3 -c "import secrets; print(secrets.token_hex(32))"
openssl rand -hex 32
head -c 32 /dev/urandom | xxd -p -c 32
```

Остальные настройки (`adapter.searxng_url`, `adapter.scraper.*`, список движков) можно оставить по умолчанию.

## 3. Запустить стек

```bash
docker compose up -d
```

Первый запуск — пару минут (pull SearXNG + Valkey образов, сборка адаптера). Последующие — несколько секунд.

Проверить, что всё поднялось:

```bash
docker compose ps
```

Должны быть три сервиса в статусе `running` / `healthy`:
- `tavily-adapter` (healthcheck через `/health`)
- `searxng`
- `redis`

## 4. Проверить работу

### SearXNG

В браузере: [http://localhost:8999](http://localhost:8999) — классический SearXNG UI.

Через API:

```bash
curl "http://localhost:8999/search?q=test&format=json" | jq '.results | length'
```

### Tavily Adapter

```bash
# Поиск
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{"query": "цена bitcoin", "max_results": 3}' | jq

# Извлечение страницы в markdown
curl -X POST http://localhost:8000/extract \
  -H "Content-Type: application/json" \
  -d '{"url":"https://en.wikipedia.org/wiki/Bitcoin","size":"s"}' | jq
```

Ожидаемая структура ответа `/search`:

```json
{
  "query": "цена bitcoin",
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

Полный список эндпойнтов и параметров → [api.md](api.md).

## 5. Интеграция в код

### Вариант А. Через официальный `tavily-python`

```python
from tavily import TavilyClient

client = TavilyClient(
    api_key="anything",               # игнорируется адаптером
    base_url="http://localhost:8000"  # ← ваш адаптер
)
response = client.search(query="что такое машинное обучение", max_results=5, include_raw_content=True)
```

### Вариант Б. Локальный клиент (без HTTP)

Если код запускается на том же хосте и не хочется гонять HTTP:

```python
from simple_tavily_adapter.tavily_client import TavilyClient

client = TavilyClient()  # читает config.yaml
response = client.search(query="...", max_results=5, include_raw_content=True)
```

### Вариант В. Голый HTTP

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

## 6. Разработка адаптера без Docker

Если хочется быстро править код адаптера с hot-reload:

```bash
# SearXNG оставляем в docker (или не трогаем)
cd simple_tavily_adapter
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

В `config.yaml` на время разработки поменяйте URL SearXNG на публикуемый хост-порт:

```yaml
adapter:
  searxng_url: "http://localhost:8999"   # вместо http://searxng:8080
```

И запускайте:

```bash
uvicorn main:app --reload --port 8000
```

> После разработки **верните** `searxng_url` в `http://searxng:8080`, иначе в Docker-контейнере адаптер не найдёт SearXNG (имя `localhost` в контейнере указывает на сам контейнер).

## Следующие шаги

- [api.md](api.md) — полный формат запросов и ответов
- [operations.md](operations.md) — логи, рестарт, отладка, траблшутинг
- [architecture.md](architecture.md) — как всё устроено внутри
