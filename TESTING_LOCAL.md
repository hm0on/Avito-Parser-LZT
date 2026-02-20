# Локальный запуск и тестирование

## Можно ли запустить без прокси?

**Да.** Прокси необязателен. Если `PROXY_URL` и `PROXY_FILE` не заданы — Playwright
запускается напрямую. Для первого теста на своей машине прокси не нужен.

---

## Быстрая проверка без БД (smoke_test.py)

Проверяет каждый коллектор и парсер в изоляции — **без Docker, без PostgreSQL**.

### Установка (один раз)

```bash
pip install -r requirements.txt
playwright install chromium
```

### Запуск

```bash
# Все компоненты
python smoke_test.py

# Только один коллектор
python smoke_test.py --only twogis
python smoke_test.py --only avito
python smoke_test.py --only fssp

# Несколько через запятую
python smoke_test.py --only twogis,avito,yandex
python smoke_test.py --only vk,flamp,otzovik
```

### Что тестируется

| Компонент | Что делает |
|-----------|-----------|
| `twogis`  | Playwright → 2gis.ru, перехватывает API-ключ, запрашивает каталог |
| `avito`   | Playwright → avito.ru, скрейпит листинг |
| `yandex`  | Playwright → yandex.ru/maps, скрейпит листинг |
| `vk`      | Playwright → m.vk.com, скрейпит стену группы |
| `fssp`    | httpx POST → fssp.gov.ru, парсит HTML таблицу |
| `flamp`   | httpx → flamp API / HTML fallback |
| `otzovik` | httpx → otzovik.com HTML scrape |

### Примерный вывод

```
Avito Parser — Smoke Test
Тестируем: twogis, avito, yandex, vk, fssp, flamp, otzovik
Ключевое слово: «бурение скважин»
Прокси: не настроен (прямое подключение)

2GIS — Playwright key capture + catalog API
  ✓ 2gis.collect  5 компаний: БурПро, АкваСкважина, ИП Петров  [18.2s]
    Первая компания: БурПро
    Телефоны: ['+7 (913) 123-45-67']
    Рейтинг: 4.8  Отзывов: 12

Avito — Playwright scraper
  ✓ avito.collect  3 компании: ВодоСервис, СкважинаОмск, ...  [22.4s]

...

──────────────────────────────────────────────────
Итого: 7/7 прошли
  ✓ 2gis.collect
  ✓ avito.collect
  ✓ yandex.collect
  ✓ vk.parse
  ✓ fssp.check
  ✓ flamp.parse
  ✓ otzovik.parse

Все проверки прошли успешно!
```

---

## Полный прогон пайплайна (с БД, 1 ключевое слово)

Если хочется прогнать весь пайплайн (сбор → обогащение → дедупликация → AI) на малом объёме данных:

### 1. Поднять только БД (без Docker Compose для API)

```bash
# Если есть локальный PostgreSQL:
createdb avito_parser
# Затем прописать в .env:
# DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/avito_parser

# Или поднять только контейнер с БД:
docker compose up -d db
```

### 2. Применить миграции

```bash
alembic upgrade head
```

### 3. Запустить пайплайн с одним ключевым словом

Создать временный файл `run_once.py`:

```python
import asyncio
from src.pipeline.runner import PipelineRunner
from src.collectors.base import AbstractCollector
from src.config import settings

# Подменяем keywords — берём только одно слово
_orig_run = AbstractCollector.run

async def _limited_run(self, keywords=None):
    return await _orig_run(self, keywords=["бурение скважин"])

AbstractCollector.run = _limited_run

asyncio.run(PipelineRunner().run())
```

```bash
python run_once.py
```

### 4. Посмотреть результаты

```bash
# Через API (если поднят FastAPI):
curl localhost:8000/companies | python -m json.tool | head -100

# Или напрямую в БД:
psql avito_parser -c "SELECT name_normalized, risk_level, reviews_count FROM companies_omsk_clean LIMIT 10;"
```

---

## Юнит-тесты (без сети, без БД)

```bash
pytest tests/ -v
# 115 тестов, ~30 секунд, не требуют интернета
```

---

## Настройка .env для локального теста

Минимальный `.env` для smoke-теста (прокси не нужен):

```env
# БД (нужна только для полного пайплайна)
DATABASE_URL=postgresql+asyncpg://avito:avito@localhost:5432/avito_parser

# OpenAI (нужен только для AI-суммаризации на Stage 4-5)
OPENAI_API_KEY=sk-...

# DaData (для обогащения ИНН/ОГРН)
DADATA_API_KEY=...
DADATA_SECRET_KEY=...

# Прокси — оставить пустым для прямого подключения
PROXY_URL=
PROXY_FILE=
```

Если `OPENAI_API_KEY` не задан — AI-этапы вернут пустую суммаризацию, но не упадут.

---

## Частые проблемы

| Ошибка | Причина | Решение |
|--------|---------|---------|
| `TimeoutError` на 2gis/avito | Сайт медленно грузится | Нормально, попробуй ещё раз |
| `Error: Could not find Chromium` | Playwright не установлен | `playwright install chromium` |
| `ModuleNotFoundError: src` | Запуск не из корня проекта | `cd /путь/к/Avito_Parser && python smoke_test.py` |
| 2GIS вернул 0 компаний | Ключ не перехвачен | 2gis.ru мог не загрузить XHR за timeout — повтори |
| ФССП — CAPTCHA | SmartCaptcha включена | Скрипт автоматически переключится на Playwright |
