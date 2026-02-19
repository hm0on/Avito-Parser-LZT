# Runbook — Avito Parser

## Быстрый старт

### 1. Клонировать и настроить окружение

```bash
git clone <repo>
cd Avito_Parser
make env         # копирует .env.example → .env
```

Заполните `.env`:

```env
OPENAI_API_KEY=sk-...
TWOGIS_API_KEY=...
DADATA_API_KEY=...
DADATA_SECRET_KEY=...
FSSP_API_TOKEN=...
PROXY_URL=http://user:pass@proxy.host:port   # опционально
```

### 2. Запустить сервисы

```bash
make build
make up
make migrate     # создать таблицы
```

### 3. Запустить пайплайн вручную

```bash
make run-pipeline
```

### 4. Проверить API

```bash
curl http://localhost:8000/health
curl http://localhost:8000/companies
curl "http://localhost:8000/exports?format=csv" -o out.csv
```

---

## Сервисы Docker Compose

| Service | Port | Описание |
|---|---|---|
| db | 5432 | PostgreSQL 16 |
| api | 8000 | FastAPI (uvicorn) |
| scheduler | — | APScheduler (weekly cron) |
| collector | — | Manual-only pipeline runner |

---

## Управление миграциями

```bash
# Применить все миграции
make migrate

# Откатить последнюю
make migrate-down

# Создать новую (autogenerate)
make migrate-create msg="add_field_x"

# Подключиться к БД
make shell-db
```

---

## Пайплайн (5 этапов)

```
Stage 1: Collect    — Avito + 2GIS + Yandex → companies_raw_omsk
Stage 2: Enrich     — Normalize phones/addresses + Registry checks → companies_enriched
Stage 3: Deduplicate— INN-merge + rapidfuzz composite → CanonicalCard
Stage 4: AI         — GPT-4o-mini summarization + Rule-based risk → risk_level
Stage 5: Write      — CanonicalCard → companies_omsk_clean
```

---

## Расписание (scheduler)

По умолчанию запускается **каждый понедельник в 03:00** (UTC).

Изменить в `.env`:
```env
SCHEDULER_CRON=0 3 * * 1   # minute hour day month weekday
```

---

## Мониторинг

Все компоненты используют `structlog` (JSON-формат в production).

```bash
# Просмотр логов
make logs

# Логи отдельного сервиса
docker compose logs -f api
docker compose logs -f scheduler
```

---

## Пороги

| Parameter | Default | Описание |
|---|---|---|
| DEDUP_THRESHOLD | 85 | Минимальный composite score для merge |
| CONFIDENCE_THRESHOLD | 70 | Ниже → manual_review_required = true |
| MAX_REVIEWS_PER_COMPANY | 200 | Лимит отзывов для AI суммаризации |

---

## Troubleshooting

### 2GIS не возвращает данные
- Проверить TWOGIS_API_KEY
- Убедиться, что region_id = 4504222397119399 (Омск)

### Avito/Yandex блокируют запросы
- Настроить PROXY_URL в .env
- Проверить playwright-stealth установлен: `playwright install chromium`

### Ошибки DaData
- Проверить лимит запросов (10K/месяц на бесплатном тарифе)
- Убедиться что DADATA_SECRET_KEY задан

### manual_review_required = true у всех записей
- Уменьшить CONFIDENCE_THRESHOLD или
- Добавить API-ключи для реестровых проверок

### Pipeline завершается без ошибок, но таблица пустая
- Убедиться, что `make migrate` выполнен
- Проверить логи коллекторов: `docker compose logs -f collector`
