## Что это

Короткая инструкция по развертыванию проекта на сервере и основным operational-командам.

Связанные документы:

- [api.md](api.md)
- [schema.md](schema.md)
- [project_structure.md](project_structure.md)

---

## Сервисы

- `db` — PostgreSQL
- `api` — FastAPI API
- `scheduler` — еженедельный автозапуск
- `collector` — ручной запуск pipeline и отдельных раннеров

---

## Первый запуск на сервере

```bash
mkdir -p /opt/avito_parser
cd /opt/avito_parser
make env
nano .env
mkdir -p storage/proxies
```

Заполнить:

- `.env`
- `storage/proxies/avito.txt`
- `storage/proxies/yandex.txt`
- `storage/proxies/twogis.txt`
- `storage/proxies/enrichment.txt`
- `storage/proxies/openai.txt`
- `storage/proxies/website.txt`

Формат строки прокси:

```text
scheme://user:pass@host:port
```

---

## Deploy на текущий сервер

Этот сервер сейчас обновляется не через `git pull`, а через file sync.

Из локального репозитория:

```bash
rsync -az \
  --exclude '.env' \
  --exclude 'storage/' \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude '.git/' \
  ./ root@<server_ip>:/opt/avito_parser/
```

Потом на сервере:

```bash
cd /opt/avito_parser
make build
make verify-collector-image
docker compose up -d db
make migrate
make up
```

Проверка:

```bash
docker compose ps
curl http://localhost:8000/health
```

Swagger:

- `http://<server_ip>:8000/docs`
- `http://<server_ip>:8000/redoc`

---

## Enrichment-only rebuild из уже готового raw

Если `avito`, `yandex`, `2gis` уже `completed` за нужную неделю, полный pipeline не нужен.

Достаточно:

```bash
docker rm -f enrichment-run 2>/dev/null || true
make run-enrichment-detached
```

Логи:

```bash
docker logs -f --tail=200 enrichment-run
```

Этот режим:

- не трогает source-этапы
- не удаляет raw-данные
- пересобирает только `companies_enriched` и `companies_omsk_clean`

---

## Полный pipeline

Интерактивно:

```bash
make run-pipeline
```

В фоне:

```bash
make run-pipeline-detached
```

Логи:

```bash
docker logs -f --tail=200 pipeline-run
```

Остановка:

```bash
docker stop pipeline-run && docker rm pipeline-run
```

---

## Отдельные этапы

```bash
make run-avito
make run-yandex
make run-twogis
make run-enrichment
```

В фоне:

```bash
make run-enrichment-detached
docker logs -f --tail=200 enrichment-run
```

---

## Smoke runs

По 20 компаний на источник:

```bash
make run-smoke-60
make run-smoke-60-detached
```

По 30 компаний на источник:

```bash
make run-smoke-90
make run-smoke-90-detached
```

Логи:

```bash
docker logs -f --tail=200 pipeline-smoke-60
docker logs -f --tail=200 pipeline-smoke-90
```

---

## Автозапуск

`scheduler` запускается через `make up`.

Проверить:

```bash
make logs-scheduler
```

В логах должно быть:

- `scheduler.started`
- `next_run_time=...`

---

## Проверка БД

Быстрая команда:

```bash
make db-check
```

Подключение:

```bash
make shell-db
```

---

## Очистка данных без удаления схемы

```bash
docker compose exec -T db psql -U avito -d avito_parser -c "
TRUNCATE TABLE
  reviews_raw_omsk,
  companies_enriched,
  companies_omsk_clean,
  companies_raw_omsk,
  collection_checkpoints
CASCADE;"
```

---

## Логи

Все сервисы:

```bash
make logs
```

Только API:

```bash
make logs-api
```

Только scheduler:

```bash
make logs-scheduler
```

Только collector:

```bash
make logs-collector
```

Важно:

- `Ctrl+C` останавливает только просмотр логов
- контейнеры продолжают работать

---

## Если что-то не так

### `make: command not found`

```bash
apt-get update && apt-get install -y make
```

### `collector` мог остаться на старом коде

```bash
make verify-collector-image
```

### Нужно быстро проверить raw за текущую неделю

```bash
docker compose exec -T db psql -U avito -d avito_parser -c "
SELECT source, COUNT(*)
FROM companies_raw_omsk
WHERE collection_week_start = DATE '2026-03-02'
GROUP BY source
ORDER BY source;"
```
