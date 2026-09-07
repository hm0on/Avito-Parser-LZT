# API Reference

## Base URL

Local:

```text
http://localhost:8000
```

Production example:

```text
http://localhost:8000
```

Docs:

- Swagger UI: `/docs`
- ReDoc: `/redoc`
- Healthcheck: `GET /health`

---

## Layers

В проекте есть 3 публичных слоя данных:

1. `clean` слой
   - маршруты `/companies...`
   - это итоговая таблица `companies_omsk_clean`
   - здесь уже есть enrichment, risk, weekly snapshot
   - автоматического merge больше нет: одна clean-карточка соответствует одной eligible source/enriched записи
   - похожие компании отдаются через `similar_company_ids`

2. `raw` слой
   - маршруты `/raw/...`
   - это прямой просмотр `companies_raw_omsk` и `reviews_raw_omsk`
   - без дедупликации, без clean-агрегации, без merge-override

3. `enriched` слой
   - маршруты `/enriched/...`
   - это прямой просмотр `companies_enriched` в связке с raw-источником
   - enrichment уже выполнен, но clean-build, manual merge и summary override здесь не применяются

---

## Default behavior of `/companies`

По умолчанию список clean-компаний фильтруется так:

- `legal_verified=true`
- `latest_week=true`

То есть без параметров API показывает только верифицированные записи последней недели.

---

## Source filter

Параметр `source` работает по `merged_sources`.

Поддерживаемые значения:

- `avito`
- `2gis`
- `yandex`

Пример:

```bash
curl "http://localhost:8000/companies?source=2gis"
```

---

## GET /companies

Список clean-компаний.

### Query params

| Param | Type | Default | Notes |
|---|---|---:|---|
| `region` | string | `null` | Зарезервирован, сейчас не применяется |
| `risk` | string | `null` | `green` / `yellow` / `red` |
| `min_rating` | float | `null` | `0..5` |
| `service` | string | `null` | Поиск по `name_normalized` |
| `source` | string | `null` | `avito` / `2gis` / `yandex` |
| `manual_review` | bool | `null` | Фильтр по флагу ручной проверки |
| `legal_verified` | bool | `true` | Фильтр по юр. верификации |
| `min_relevance_score` | float | `null` | `0..1` |
| `geo_verified` | bool | `null` | Фильтр по geo |
| `week_start` | date | `null` | Понедельник нужной недели |
| `latest_week` | bool | `true` | Если `week_start` не передан |
| `page` | int | `1` | `>=1` |
| `page_size` | int | `20` | `1..100` |

### Response fields

Ключевые поля списка:

- `id`
- `name_normalized`
- `source_name_primary`
- `legal_name`
- `legal_verified`
- `relevance_score`
- `geo_verified`
- `phones`
- `addresses`
- `average_rating`
- `reviews_count`
- `risk_level`
- `merged_sources`
- `source_links`
- `similar_company_ids`
- `pipeline_week_start`
- `manual_review_required`

### Example

```bash
curl "http://localhost:8000/companies?source=avito&latest_week=true&page=1&page_size=20"
```

---

## GET /companies/weeks

Доступные недели в clean-слое.

```bash
curl "http://localhost:8000/companies/weeks"
```

---

## GET /companies/{company_id}

Полная clean-карточка компании.

Кроме полей списка возвращает также:

- `inn`
- `ogrn`
- `quality_flags`
- `emails`
- `contacts_json`
- `reviews_sample`
- `summary_review`
- `risk_reasons`
- `source_records`
- `checks`
- `created_at`
- `last_updated`

### Example

```bash
curl "http://localhost:8000/companies/346d94ed-a334-4e11-8525-d92bcc24e889"
```

---

## POST /companies/{company_id}/delete

Удаляет компанию только из clean-слоя.

Важно:

- raw/enriched не удаляются
- при будущем rerun эта компания может появиться снова

### Example

```bash
curl -X POST "http://localhost:8000/companies/<COMPANY_ID>/delete"
```

---

## POST /companies/{company_id}/avito_phone

Берет Avito source-link из clean-карточки и запрашивает актуальный номер через `spfa.ru`.

### Example

```bash
curl -X POST "http://localhost:8000/companies/<COMPANY_ID>/avito_phone"
```

### Response

```json
{
  "company_id": "...",
  "avito_url": "https://www.avito.ru/...",
  "ad_id": "3643806995",
  "phone": "+79382224014",
  "provider": "spfa",
  "provider_status": 200,
  "raw_response": {
    "success": true
  }
}
```

---

## POST /companies/{company_id}/manual_review

Ручная правка риска и снятие `manual_review_required`.

### Request body

```json
{
  "reviewer_notes": "Проверено вручную",
  "resolved_risk_level": "green",
  "clear_manual_flag": true
}
```

---

## POST /companies/{company_id}/summarize_reviews

Ручная суммаризация отзывов по одной clean-карточке.

Важно:

- автоматическая суммаризация в pipeline отключена
- summary считается только по запросу
- результат сохраняется в БД и переживает rerun
- если `summary_review` уже есть и `force=false`, пересчета не будет

### Request body

```json
{
  "force": false
}
```

### Example

```bash
curl -X POST "http://localhost:8000/companies/<COMPANY_ID>/summarize_reviews" \
  -H "Content-Type: application/json" \
  -d '{"force": true}'
```

### Response

Возвращает полную обновленную `CompanyDetail`.

---

## POST /companies/merge

Ручное объединение нескольких clean-компаний в одну synthetic master-card.

Что происходит:

- создается persistent merge-group
- при rebuild clean-слоя исходные компании скрываются
- в `/companies` остается только master-card
- merge переживает будущие rerun-ы
- raw/enriched при этом не меняются

### Request body

```json
{
  "company_ids": [
    "uuid-1",
    "uuid-2"
  ],
  "primary_company_id": "uuid-1"
}
```

### Response

```json
{
  "master_company_id": "uuid-master",
  "merged_company_ids": [
    "uuid-1",
    "uuid-2"
  ],
  "company": {
    "id": "uuid-master",
    "name_normalized": "ООО Пример",
    "similar_company_ids": []
  }
}
```

### Notes

- все `company_ids` должны относиться к одной `pipeline_week_start`
- manual merge — единственный способ получить synthetic master-card
- автоматический dedup больше не объединяет компании в один clean-row

---

## GET /raw/weeks

Список доступных недель в raw-слое.

```bash
curl "http://localhost:8000/raw/weeks"
```

---

## GET /raw/companies

Список raw-компаний из `companies_raw_omsk`.

### Query params

| Param | Type | Default | Notes |
|---|---|---:|---|
| `source` | string | `null` | `avito` / `2gis` / `yandex` |
| `week_start` | date | `null` | Понедельник нужной недели |
| `latest_week` | bool | `true` | Если `week_start` не передан |
| `page` | int | `1` | `>=1` |
| `page_size` | int | `20` | `1..200` |

### Response fields

- `id`
- `source`
- `source_id`
- `source_link`
- `name_raw`
- `phones`
- `emails`
- `addresses`
- `contacts_json`
- `inn`
- `ogrn`
- `average_rating`
- `reviews_count`
- `collection_week_start`
- `collected_at`
- `is_processed`

### Example

```bash
curl "http://localhost:8000/raw/companies?source=2gis&latest_week=true"
```

---

## GET /raw/companies/{raw_company_id}

Детальная raw-карточка, включая `raw_payload`.

```bash
curl "http://localhost:8000/raw/companies/<RAW_COMPANY_ID>"
```

---

## GET /raw/companies/{raw_company_id}/reviews

Сырые отзывы конкретной raw-компании.

### Query params

- `page`
- `page_size`

```bash
curl "http://localhost:8000/raw/companies/<RAW_COMPANY_ID>/reviews?page=1&page_size=100"
```

---

## GET /raw/reviews

Просмотр raw-отзывов из `reviews_raw_omsk`.

### Query params

| Param | Type | Default | Notes |
|---|---|---:|---|
| `source` | string | `null` | `avito` / `2gis` / `yandex` |
| `week_start` | date | `null` | Понедельник нужной недели |
| `latest_week` | bool | `true` | Если `week_start` не передан |
| `company_raw_id` | uuid | `null` | Ограничить одной raw-компанией |
| `page` | int | `1` | `>=1` |
| `page_size` | int | `20` | `1..500` |

### Example

```bash
curl "http://localhost:8000/raw/reviews?source=yandex&latest_week=true&page=1&page_size=100"
```

---

## GET /enriched/weeks

Список доступных недель в enriched-слое.

```bash
curl "http://localhost:8000/enriched/weeks"
```

---

## GET /enriched/companies

Список enriched-компаний из `companies_enriched`.

Это слой после enrichment, но до clean-build.

### Query params

| Param | Type | Default | Notes |
|---|---|---:|---|
| `source` | string | `null` | `avito` / `2gis` / `yandex` |
| `legal_verified` | bool | `null` | Фильтр по юр. верификации |
| `manual_review` | bool | `null` | Фильтр по флагу ручной проверки |
| `min_relevance_score` | float | `null` | `0..1` |
| `week_start` | date | `null` | Понедельник нужной недели |
| `latest_week` | bool | `true` | Если `week_start` не передан |
| `page` | int | `1` | `>=1` |
| `page_size` | int | `20` | `1..200` |

### Response fields

- `id`
- `raw_id`
- `source`
- `source_id`
- `source_link`
- `name_raw`
- `name_normalized`
- `legal_verified`
- `legal_match_method`
- `legal_match_score`
- `relevance_score`
- `confidence_score`
- `manual_review_required`
- `pipeline_week_start`
- `enriched_at`

### Example

```bash
curl "http://localhost:8000/enriched/companies?source=2gis&latest_week=true"
```

---

## GET /enriched/companies/{enriched_id}

Полная enriched-карточка.

Кроме полей списка возвращает также:

- `raw_payload`
- `raw_phones`
- `raw_emails`
- `raw_addresses`
- `raw_contacts_json`
- `raw_inn`
- `raw_ogrn`
- `raw_average_rating`
- `raw_reviews_count`
- `collection_week_start`
- `inn`
- `ogrn`
- `entity_type`
- `phones_normalized`
- `addresses_parsed`
- `checks`
- `relevance_details`

### Example

```bash
curl "http://localhost:8000/enriched/companies/<ENRICHED_ID>"
```

---

## GET /exports

Экспорт clean-слоя в `csv` или `json`.

### Query params

Поддерживает те же основные фильтры, что и `/companies`:

- `format=csv|json`
- `risk`
- `min_rating`
- `source`
- `manual_review`
- `legal_verified`
- `min_relevance_score`
- `geo_verified`
- `week_start`
- `latest_week`

### Example

```bash
curl "http://localhost:8000/exports?format=json&source=avito"
```

---

## Important behavior notes

- raw API ничего не знает о manual merge и summary override
- enriched API ничего не знает о manual merge и summary override
- clean API строится из weekly rebuild
- `similar_company_ids` — это детектор похожих компаний, а не auto-merge
- если company удалена из clean через `/delete`, raw и enriched остаются
- если summary посчитана через `/summarize_reviews`, она сохраняется и переживает rerun
