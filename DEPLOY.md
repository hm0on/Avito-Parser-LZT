# Деплой Avito Parser на сервер

## Требования к серверу

- Ubuntu 22.04+ / Debian 12+
- 4+ ГБ RAM (рекомендуется 16+ ГБ для параллельного парсинга)
- Docker + Docker Compose v2
- SSH доступ (root или sudo)

## 1. Подготовка сервера

```bash
# Установить Docker (если не установлен)
apt-get update && apt-get install -y docker.io docker-compose-v2

# Проверить
docker --version
docker compose version
```

## 2. Копирование проекта на сервер

С локальной машины:

```bash
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='.venv' --exclude='venv' \
  ./ root@<SERVER_IP>:/opt/avito_parser/
```

## 3. Настройка .env

На сервере:

```bash
cd /opt/avito_parser
cp .env.production .env
nano .env  # заполнить API-ключи
```

**ВАЖНО:** В `.env` должно быть:
- `DATABASE_URL=postgresql+asyncpg://avito:avito@db:5432/avito_parser` (хост `db`, НЕ `localhost`)
- `YANDEX_DEBUG=false`
- `TWOGIS_DEBUG_BROWSER=false`

Заполнить ключи:
- `OPENAI_API_KEY` — OpenAI API
- `DADATA_API_KEY` + `DADATA_SECRET_KEY` — DaData
- `SX_PROXY_API_KEY` — SX.org прокси
- `AVITO_COOKIES_API_KEY` — spfa.ru
- `SERPAPI_KEY` — SerpAPI (для поиска отзывов)

## 4. Сборка и запуск

```bash
cd /opt/avito_parser

# Собрать все образы
docker compose build

# Запустить БД, дождаться ready
docker compose up -d db
sleep 5

# Применить миграции
docker compose run --rm api alembic upgrade head

# Запустить API + планировщик
docker compose up -d api scheduler

# Проверить что всё работает
docker compose ps
curl localhost:8000/companies
```

## 5. Запуск пайплайна

```bash
cd /opt/avito_parser

# Ручной запуск полного пайплайна (сбор → обогащение → дедупликация → AI → экспорт)
docker compose run --rm collector python -m src.pipeline.runner
```

Планировщик автоматически запускает пайплайн по крону (по умолчанию: каждый понедельник в 03:00).

## 6. Мониторинг

```bash
# Логи в реальном времени
docker compose logs -f api
docker compose logs -f scheduler
docker compose logs -f collector   # во время ручного запуска

# Статус контейнеров
docker compose ps

# Подключиться к БД
docker compose exec db psql -U avito -d avito_parser
```

## 7. Экспорт данных

```bash
# CSV
curl "http://localhost:8000/exports?format=csv" -o export.csv

# JSON (список компаний)
curl "http://localhost:8000/companies"

# Извне (замени SERVER_IP)
curl "http://<SERVER_IP>:8000/companies"
```

## 8. Обновление кода

С локальной машины:

```bash
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='.venv' --exclude='venv' --exclude='.env' \
  ./ root@<SERVER_IP>:/opt/avito_parser/
```

На сервере:

```bash
cd /opt/avito_parser
docker compose build             # пересобрать образы
docker compose up -d api scheduler  # перезапустить сервисы
# Если изменились миграции:
docker compose run --rm api alembic upgrade head
```

## 9. Настройка производительности

Все параметры параллелизма настраиваются через `.env`:

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TWOGIS_FIRM_WORKERS` | 4 | Параллельных браузеров для 2GIS (~300 МБ RAM каждый) |
| `TWOGIS_TABS_PER_BROWSER` | 3 | Вкладок на браузер |
| `ENRICH_CONCURRENCY` | 5 | Параллельных обогащений (Stage 2) |
| `AI_CONCURRENCY` | 5 | Параллельных AI-запросов (Stage 4-5) |
| `AVITO_MAX_KEYWORDS` | 20 | Параллельных ключевых слов для Avito |
| `YANDEX_MAX_KEYWORDS` | 20 | Параллельных ключевых слов для Yandex |
| `TWOGIS_MAX_KEYWORDS` | 20 | Параллельных ключевых слов для 2GIS |
| `COLLECTOR_MAX_KEYWORDS` | 3 | Глобальный fallback для HTTP-коллекторов без отдельного лимита |
| `PLAYWRIGHT_MAX_KEYWORDS` | 2 | Глобальный fallback для Playwright-коллекторов без отдельного лимита |

Пример для мощного сервера (62 ГБ RAM):

```env
TWOGIS_FIRM_WORKERS=10
TWOGIS_TABS_PER_BROWSER=5
ENRICH_CONCURRENCY=15
AI_CONCURRENCY=10
AVITO_MAX_KEYWORDS=20
YANDEX_MAX_KEYWORDS=20
TWOGIS_MAX_KEYWORDS=20
COLLECTOR_MAX_KEYWORDS=6
PLAYWRIGHT_MAX_KEYWORDS=4
```

## 10. Остановка

```bash
cd /opt/avito_parser

# Остановить всё
docker compose down

# Остановить с удалением данных БД
docker compose down -v
```

## Решение проблем

**`no configuration file provided: not found`** — ты не в папке проекта. Сделай `cd /opt/avito_parser`.

**`alembic.config: No module named`** — в папке `alembic/` есть `__init__.py`, который конфликтует с пакетом. Удали его: `rm alembic/__init__.py` и пересобери: `docker compose build`.

**`playwright install` таймаутится** — медленный интернет. Попробуй ещё раз: `docker compose build collector --no-cache`.

**Контейнер падает сразу** — проверь логи: `docker compose logs api` или `docker compose logs collector`.
