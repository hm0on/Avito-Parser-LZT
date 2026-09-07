## Overview

Проект состоит из 4 основных слоев:

1. `collectors` — сбор компаний и отзывов из `Avito`, `Yandex`, `2GIS`
2. `pipeline + enrichment` — фильтрация, проверки, дедупликация, суммаризация, risk-assessment
3. `database + api` — хранение weekly-срезов и выдача результатов через API
4. `docker + scheduler` — production runtime и автоматический еженедельный запуск

---

## Top-Level Structure

```text
Avito_Parser/
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── requirements.txt
├── alembic/
├── docs/
├── scheduler/
├── scripts/
├── src/
├── storage/
└── tests/
```

### Root files

- `Dockerfile` — production image build
- `docker-compose.yml` — сервисы `db`, `api`, `scheduler`, `collector`
- `Makefile` — команды сборки, миграций, запуска и smoke/full pipeline
- `requirements.txt` — runtime dependencies
- `run_once.py` — упрощенный локальный запуск
- `proxy_generator.py` — генерация proxy-пулов

---

## Source Code Layout

```text
src/
├── ai/
├── api/
├── collectors/
├── database/
├── deduplication/
├── enrichment/
├── pipeline/
├── relevance/
├── services/
├── config.py
└── proxy.py
```

### `src/config.py`

Центральный конфиг проекта:

- env-переменные
- лимиты параллелизма
- API keys
- настройки proxy
- scheduler/pipeline toggles

### `src/proxy.py`

Управление proxy-пулами по сервисам:

- `avito`
- `yandex`
- `twogis`
- `enrichment`
- `openai`
- `website`

---

## Collectors

```text
src/collectors/
├── avito.py
├── yandex.py
├── twogis.py
├── base.py
└── avito_proxy_precheck.py
```

Назначение:

- `avito.py` — поиск объявлений, карточек, отзывов, контактов Avito
- `yandex.py` — поиск карточек Yandex Maps и парс отзывов
- `twogis.py` — поиск карточек 2GIS и парс отзывов
- `base.py` — общие dataclass/контракты для source collectors
- `avito_proxy_precheck.py` — предварительная оценка и ранжирование Avito proxy

---

## Pipeline

```text
src/pipeline/
├── runner.py
├── source_runner.py
├── avito_runner.py
├── yandex_runner.py
├── twogis_runner.py
├── enrichment_runner.py
└── common.py
```

Назначение:

- `runner.py` — полный pipeline orchestration
- `source_runner.py` — общий runner для source-этапов
- `avito_runner.py` — запуск только Avito
- `yandex_runner.py` — запуск только Yandex
- `twogis_runner.py` — запуск только 2GIS
- `enrichment_runner.py` — enrichment, дедупликация, summary, risk, запись в clean
- `common.py` — DB helpers, weekly checkpoint logic, shared pipeline functions

### Full pipeline flow

```text
avito_runner
   + yandex_runner
   + twogis_runner
           ↓
   raw companies + raw reviews
           ↓
     enrichment_runner
           ↓
  companies_enriched + clean cards
           ↓
           API
```

---

## Relevance Layer

```text
src/relevance/
├── __init__.py
├── domain_filter.py
├── geo_filter.py
└── llm_relevance.py
```

Назначение:

- `domain_filter.py` — отсев нерелевантных тематик
- `geo_filter.py` — отсев компаний вне Омска и Омской области
- `llm_relevance.py` — LLM-арбитраж спорных кейсов
- `__init__.py` — сборка итогового relevance-decision

Эта логика применяется до записи source batch в raw-БД.

---

## Enrichment Layer

```text
src/enrichment/
├── enricher.py
├── normalizers.py
├── review_searcher.py
├── website_scanner.py
├── registries/
└── review_parsers/
```

### Core files

- `enricher.py` — главный orchestrator enrichment одной компании
- `normalizers.py` — нормализация телефонов, адресов и контактов
- `review_searcher.py` — поиск внешних отзывов
- `website_scanner.py` — поиск и анализ персональных сайтов компаний

### `registries/`

Проверки и обогащение через внешние реестры и сервисы:

- `listorg`
- `rusprofile`
- `dadata`
- `arbitr`
- `efrsb`
- `eis`
- `nostroy`
- `fssp`
- `openai_company_fallback`

### `review_parsers/`

Парсеры внешних отзывов:

- `flamp`
- `vk`
- `otzovik`

---

## AI Layer

```text
src/ai/
├── summarizer.py
└── risk_assessor.py
```

Назначение:

- `summarizer.py` — суммаризация отзывов в итоговую текстовую выжимку
- `risk_assessor.py` — оценка рисков компании на основе проверок и сигналов

---

## Deduplication

```text
src/deduplication/
└── deduplicator.py
```

Назначение:

- объединяет raw/enriched записи в одну canonical company card
- учитывает `INN`, `OGRN`, телефоны, similarity names
- собирает:
  - `merged_sources`
  - `source_links`
  - `source_records`

---

## Database Layer

```text
src/database/
├── models.py
└── session.py
```

### `models.py`

Основные таблицы:

- `companies_raw_omsk`
- `reviews_raw_omsk`
- `companies_enriched`
- `companies_omsk_clean`
- `collection_checkpoints`

### Data flow in DB

```text
companies_raw_omsk
        ↓
reviews_raw_omsk
        ↓
companies_enriched
        ↓
companies_omsk_clean

collection_checkpoints
  └── weekly statuses for avito / yandex / 2gis / enrichment
```

---

## API Layer

```text
src/api/
├── main.py
├── schemas.py
├── query_filters.py
└── routes/
```

### Core files

- `main.py` — FastAPI entrypoint
- `schemas.py` — response/request schemas
- `query_filters.py` — общие API filters

### `routes/`

Основные endpoint groups:

- список компаний
- detail карточки компании
- фильтрация по неделям
- фильтрация по источнику
- удаление компании из `clean`
- on-demand получение актуального номера Avito через `SPFA`
- export endpoints

---

## Services

```text
src/services/
└── spfa.py
```

Назначение:

- интеграция с `spfa.ru` для получения актуального номера по `Avito ad id`

---

## Scheduler

```text
scheduler/
└── scheduler.py
```

Назначение:

- weekly cron запуск пайплайна
- timezone-aware scheduling
- single-instance execution

---

## Migrations

```text
alembic/
├── env.py
└── versions/
    ├── 001_initial.py
    ├── 002_weekly_runners.py
    ├── 003_add_source_links.py
    └── 004_precision_fields.py
```

Назначение:

- версия схемы БД
- weekly pipeline support
- source links
- precision/relevance/legal fields

---

## Storage

```text
storage/
├── proxies/
├── avito_debug/
├── captcha_debug*/
└── *.html / *.json / *.log
```

### `storage/proxies/`

Актуальные proxy pools:

- `avito.txt`
- `yandex.txt`
- `twogis.txt`
- `enrichment.txt`
- `openai.txt`
- `website.txt`
- `default.txt`

### Остальное в `storage/`

Локальные артефакты разработки и отладки:

- html/json dumps
- debug screenshots
- temporary logs
- captcha experiments

Для production-логики критичен в первую очередь каталог `storage/proxies/`.

---

## Tests

```text
tests/
├── ai/
├── api/
├── collectors/
├── enrichment/
├── fixtures/
├── services/
└── unit/
```

Назначение:

- `tests/collectors/` — парсинг источников и отзывов
- `tests/enrichment/` — review parsers, registry checks, fallback logic
- `tests/api/` — API routes and filters
- `tests/services/` — external service clients, например `SPFA`
- `tests/unit/` — proxy, pipeline, relevance, dedup, AI, source links
- `tests/fixtures/` — локальные HTML/JSON fixtures

---

## Production Runtime

```text
docker-compose services
├── db
├── api
├── scheduler
└── collector
```

### Services

- `db` — PostgreSQL
- `api` — FastAPI
- `scheduler` — weekly automation
- `collector` — manual runtime для pipeline и отдельных runners

---

## Main Business Flow

```text
1. Collectors parse Avito / Yandex / 2GIS
2. Relevance filters remove noise
3. Raw companies and reviews are stored in weekly raw tables
4. Enricher normalizes data and runs external checks
5. Extra reviews / websites / registries are collected
6. Records are deduplicated into canonical company cards
7. AI generates summary + risk assessment
8. Final cards are written to companies_omsk_clean
9. API serves the latest or requested weekly slice
```

---

## Key Business Tables

Для заказчика наиболее важны:

- `companies_raw_omsk` — все найденные source-данные
- `reviews_raw_omsk` — все собранные отзывы
- `companies_enriched` — промежуточный enrichment слой
- `companies_omsk_clean` — финальная “белая” база для API
- `collection_checkpoints` — статусы этапов пайплайна по неделям

---

## Short Summary

Это backend-система для weekly-сбора компаний из нескольких площадок, их проверки, обогащения, дедупликации и публикации через API.  
Проект развертывается в Docker, работает по недельным срезам и поддерживает как ручной запуск пайплайна, так и автоматический запуск по расписанию.
