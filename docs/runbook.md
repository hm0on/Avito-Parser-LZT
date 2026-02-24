# Runbook — Avito Parser

## Быстрый старт

### 1. Клонировать и настроить окружение

```bash
git clone <repo>
cd Avito_Parser
make env         # копирует .env.example → .env
```

Заполните `.env` (подробности по каждому ключу — см. [api_keys.md](api_keys.md)):

```env
# Обязательные
OPENAI_API_KEY=sk-proj-...
DADATA_API_KEY=...
DADATA_SECRET_KEY=...

# Прокси (sx.org — RU для скрапинга, US для OpenAI)
SX_PROXY_API_KEY=...

# Опциональные (улучшают покрытие)
AVITO_COOKIES_API_KEY=...
SERPAPI_KEY=...
```

### 2. Установить зависимости

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Запустить сервисы

```bash
make build
make up
make migrate     # создать таблицы
```

`make up` и `make build` используют `docker-compose.yml + docker-compose.dev.yml` (локальная разработка с bind-mount/reload).
Для сервера используйте только `docker-compose.yml` (см. `DEPLOY.md`).

### 4. Тестовый прогон (30 компаний)

```bash
python3 run_once.py
```

Это запустит полный пайплайн (все 5 стадий), но ограничит выборку до 30 компаний (~10 от каждого источника). Занимает ~7–15 минут.

### 5. Полный пайплайн

```bash
make run-pipeline
```

### 6. Проверить API

```bash
curl http://localhost:8000/health
curl http://localhost:8000/companies
curl "http://localhost:8000/exports?format=csv" -o out.csv
```

---

## Тестирование отдельных компонентов

`test_tool.py` — универсальный CLI для тестирования каждого модуля по отдельности.

```bash
# Коллекторы (скрапинг)
python3 test_tool.py yandex                        # Яндекс.Карты
python3 test_tool.py twogis                        # 2GIS
python3 test_tool.py avito                         # Авито
python3 test_tool.py yandex -k "монтаж отопления"  # конкретный запрос

# Проверка по ИНН
python3 test_tool.py inn 5503098042                # DaData lookup
python3 test_tool.py registries 5503098042         # все 13 реестров

# ФССП
python3 test_tool.py fssp --inn 5503098042
python3 test_tool.py fssp --name "ООО Рога и копыта"

# Сканер сайта
python3 test_tool.py website https://example.com

# Полный цикл: сбор → обогащение
python3 test_tool.py enrich twogis
python3 test_tool.py enrich yandex -k "вентиляция"

# Все тесты разом
python3 test_tool.py all
```

---

## Сервисы Docker Compose

| Service | Port | Описание |
|---|---|---|
| db | 5432 | PostgreSQL 16 |
| api | 8000 | FastAPI (uvicorn) |
| scheduler | — | APScheduler (еженедельный cron) |
| collector | — | Manual-only pipeline runner |

---

## Пайплайн (5 этапов)

```
Stage 1: Collect
  Avito + 2GIS + Yandex → companies_raw_omsk + reviews_raw_omsk
  + дополнительный сбор телефонов и отзывов через enrich_phones/enrich_reviews

Stage 2: Enrich (двухпроходный)
  Pass 1 (11 чекеров параллельно):
    DaData, ФНС ПБ, ФССП, Арбитраж, ЕФРСБ, ЕИС, НОСТРОЙ, МСП, РНП, Проверки, НПД
  Pass 2 (2 чекера, зависят от pass 1):
    Дисквалификация (нужен ФИО директора), Массовый адрес (нужен юр. адрес)
  + Поиск отзывов (Flamp, VK, Отзовик) → reviews_raw_omsk
  → companies_enriched

Stage 3: Deduplicate
  INN-merge + rapidfuzz composite (0.6 name + 0.3 phone + 0.1 region)
  → CanonicalCard

Stage 4: AI Summarize + Risk Assess
  GPT-4o-mini суммаризация отзывов + правила риска (red/yellow/green)

Stage 5: Write
  → companies_omsk_clean (финальная таблица)
```

---

## Прокси

Проект использует **sx.org** для всех прокси:

| Назначение | Страна | Где используется |
|---|---|---|
| Скрапинг | **RU** | Avito, 2GIS, Yandex, VK, Flamp, ФССП |
| OpenAI API | **US** | Суммаризация отзывов (обход блокировки) |

При первом запуске sx.org автоматически создаёт прокси-порты. В логах будет:

```
sx_proxy.save_port_id  country=RU  port_id=12345  hint="Add SX_PROXY_PORT_IDS_RU=12345,... to .env"
sx_proxy.save_port_id  country=US  port_id=67890  hint="Add SX_PROXY_PORT_ID_US=67890 to .env"
```

Сохраните эти ID в `.env` чтобы не пересоздавать порты при каждом запуске:

```env
SX_PROXY_PORT_IDS_RU=12345,12346,12347
SX_PROXY_RU_POOL_SIZE=3
SX_PROXY_PORT_ID_US=67890
```

Fallback: если `SX_PROXY_API_KEY` не задан, используется `PROXY_URL` / `PROXY_FILE` (устаревший способ).

---

## Реестровые проверки (13 штук)

| # | Реестр | Что проверяет | Красный/Жёлтый флаг |
|---|---|---|---|
| 1 | DaData (ФНС) | ИНН, статус, название, тип | Ликвидирована/банкрот → RED |
| 2 | ФНС «Прозрачный бизнес» | Маркеры риска, руководитель, адрес | Критические маркеры → RED |
| 3 | ФССП | Исполнительные производства | >1M руб → RED, <1M → YELLOW |
| 4 | Кад.Арбитр | Арбитражные дела | ≥3 активных → RED, любые → YELLOW |
| 5 | ЕФРСБ | Банкротство | Банкрот → RED |
| 6 | ЕИС Закупки | Госзакупки | Информационно |
| 7 | НОСТРОЙ | СРО строителей | Не найден → YELLOW |
| 8 | МСП | Реестр малого/среднего бизнеса | Информационно |
| 9 | РНП | Недобросовестные поставщики | В реестре → RED |
| 10 | Проверки | Проверки госорганов | С нарушениями → YELLOW |
| 11 | НПД | Самозанятый (налог на проф. доход) | Информационно |
| 12 | Дисквалификация | Руководитель дисквалифицирован | Дисквалифицирован → RED |
| 13 | Массовый адрес | Адрес массовой регистрации | Массовый → YELLOW |

---

## Управление миграциями

```bash
make migrate                          # применить все миграции
make migrate-down                     # откатить последнюю
make migrate-create msg="add_field"   # создать новую (autogenerate)
make shell-db                         # подключиться к БД
```

---

## Расписание (scheduler)

По умолчанию запускается **каждый понедельник в 03:00** (UTC).

```env
SCHEDULER_CRON=0 3 * * 1   # minute hour day month weekday
```

---

## Пороги и настройки

### Pipeline

| Parameter | Default | Описание |
|---|---|---|
| `DEDUP_THRESHOLD` | 85 | Минимальный composite score для merge |
| `CONFIDENCE_THRESHOLD` | 70 | Ниже → `manual_review_required = true` |
| `MAX_REVIEWS_PER_COMPANY` | 200 | Лимит отзывов для AI-суммаризации |
| `ENRICH_CONCURRENCY` | 5 | Параллельных обогащений (Stage 2) |
| `AI_CONCURRENCY` | 5 | Параллельных AI-запросов (Stage 4-5) |
| `AVITO_MAX_KEYWORDS` | 20 | Параллельных ключевых слов для Avito |
| `YANDEX_MAX_KEYWORDS` | 20 | Параллельных ключевых слов для Yandex |
| `TWOGIS_MAX_KEYWORDS` | 20 | Параллельных ключевых слов для 2GIS |
| `COLLECTOR_MAX_KEYWORDS` | 3 | Глобальный fallback для HTTP-коллекторов без отдельного лимита |
| `PLAYWRIGHT_MAX_KEYWORDS` | 2 | Глобальный fallback для Playwright-коллекторов без отдельного лимита |

### 2GIS

| Parameter | Default | Описание |
|---|---|---|
| `TWOGIS_SEARCH_MAX_PAGES` | 8 | Макс. страниц поиска на запрос |
| `TWOGIS_FIRM_WORKERS` | 4 | Параллельных браузеров для скрапинга фирм |
| `TWOGIS_TABS_PER_BROWSER` | 3 | Вкладок на браузер |
| `TWOGIS_COMPANY_RETRY_ATTEMPTS` | 2 | Попыток скрапинга одной фирмы |
| `TWOGIS_RETRY_UNTIL_SUCCESS` | true | Повторять до успеха (со сменой прокси) |
| `TWOGIS_MAX_PROXY_ROTATIONS` | 30 | Макс. смен прокси |
| `TWOGIS_SEARCH_REGION` | true | Искать по всей Омской области |
| `TWOGIS_DEBUG_BROWSER` | false | Показать окно браузера |

### Yandex

| Parameter | Default | Описание |
|---|---|---|
| `YANDEX_DEBUG` | false | Показать окно браузера |
| `YANDEX_DEBUG_SLOW_MO_MS` | 0 | Замедление Playwright (мс) |
| `YANDEX_DEBUG_HOLD_SECONDS` | 0 | Пауза перед закрытием для осмотра |
| `YANDEX_BROWSER_RESTART_ATTEMPTS` | 3 | Попыток перезапуска при ошибке |
| `YANDEX_STATE_VIEW_IMMEDIATE` | true | Парсить JSON из state-view сразу |
| `YANDEX_SINGLE_PASS` | true | Один проход (без пагинации) |

---

## Мониторинг

Все компоненты используют `structlog` (JSON-формат в production).

```bash
make logs                        # все логи
docker compose logs -f api       # только API
docker compose logs -f scheduler # только scheduler
```

---

## Troubleshooting

### Прокси: 407 Proxy Authentication Required
- Проверить `SX_PROXY_API_KEY` в `.env`
- Убедиться, что sx.org аккаунт активен и есть баланс
- Попробовать пересоздать порты: удалить `SX_PROXY_PORT_ID_RU` / `SX_PROXY_PORT_ID_US` из `.env`

### OpenAI: Connection refused / timeout
- OpenAI заблокирован в РФ — нужен US-прокси через sx.org
- Проверить что `SX_PROXY_API_KEY` задан
- В логах должно быть `summarizer.using_proxy`

### 2GIS не возвращает данные
- 2GIS API-ключ не нужен (ключ перехватывается из браузера автоматически)
- Убедиться что `region_id = 4504222397119399` (Омск)
- Попробовать `TWOGIS_DEBUG_BROWSER=true` для отладки

### Avito/Yandex блокируют запросы
- Проверить что sx.org RU-прокси работают (в логах `proxy_manager.sx_ru_loaded`)
- Для Авито: настроить `AVITO_COOKIES_API_KEY` (spfa.ru)
- `playwright install chromium` — должен быть установлен

### Ошибки DaData
- Проверить лимит запросов (10K/месяц на бесплатном тарифе)
- Убедиться что `DADATA_SECRET_KEY` задан

### manual_review_required = true у всех записей
- Уменьшить `CONFIDENCE_THRESHOLD` или
- Добавить недостающие API-ключи для реестровых проверок (больше проверок → выше score)

### Pipeline завершается без ошибок, но таблица пустая
- Убедиться что `make migrate` выполнен
- Проверить логи коллекторов: `docker compose logs -f collector`
- Попробовать `python3 run_once.py` для диагностики
