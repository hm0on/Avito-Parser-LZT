# REST API Reference

Base URL: `http://localhost:8000`

Interactive docs: `http://localhost:8000/docs` (Swagger UI)

---

## GET /companies

Список компаний с фильтрацией и пагинацией.

### Query parameters

| Parameter | Type | Description |
|---|---|---|
| risk | string | green / yellow / red |
| min_rating | float | Минимальный средний рейтинг (0–5) |
| service | string | Поиск по названию (ILIKE) |
| manual_review | bool | Требует ручной проверки |
| page | int | Страница (default 1) |
| page_size | int | Размер страницы (1–100, default 20) |

### Response 200

```json
{
  "total": 142,
  "page": 1,
  "page_size": 20,
  "results": [
    {
      "id": "uuid",
      "name_normalized": "ООО СтройМонтаж",
      "inn": "5501234567",
      "entity_type": "ЮЛ",
      "phones": ["+73812123456"],
      "average_rating": 4.3,
      "reviews_count": 27,
      "risk_level": "green",
      "merged_sources": ["avito", "2gis"],
      "manual_review_required": false,
      "last_updated": "2026-02-19T03:00:00Z"
    }
  ]
}
```

---

## GET /companies/{id}

Полная карточка компании.

### Response 200

```json
{
  "id": "uuid",
  "inn": "5501234567",
  "ogrn": "1025500000001",
  "name_normalized": "ООО СтройМонтаж",
  "entity_type": "ЮЛ",
  "phones": ["+73812123456"],
  "emails": ["info@stroymontazh.ru"],
  "addresses": [{"raw": "г. Омск, ул. Ленина, 1", "city": "Омск", "street": "ул. Ленина"}],
  "average_rating": 4.3,
  "reviews_count": 27,
  "reviews_sample": [...],
  "summary_review": "Компания специализируется на...",
  "risk_level": "green",
  "risk_reasons": [],
  "source_records": ["uuid1", "uuid2"],
  "merged_sources": ["avito", "2gis"],
  "checks": {
    "dadata_fns": {"found": true, "status": "ACTIVE"},
    "fssp": {"found": false},
    "nostroy": {"found": true, "status": "active"}
  },
  "manual_review_required": false,
  "last_updated": "2026-02-19T03:00:00Z",
  "created_at": "2026-02-19T03:00:00Z"
}
```

---

## POST /companies/{id}/manual_review

Результат ручной проверки.

### Request body

```json
{
  "reviewer_notes": "Проверено: компания действующая, риски не подтверждены",
  "resolved_risk_level": "green",
  "clear_manual_flag": true
}
```

### Response 200

Обновлённая карточка (CompanyDetail).

---

## GET /exports

Экспорт всех компаний.

### Query parameters

| Parameter | Type | Description |
|---|---|---|
| format | string | csv (default) или json |
| risk | string | Фильтр по риску |
| min_rating | float | Фильтр по рейтингу |
| manual_review | bool | Фильтр |

### Response

- `Content-Type: text/csv` — CSV-файл с BOM
- `Content-Type: application/json` — JSON-массив

### CSV columns

`id, name_normalized, inn, ogrn, entity_type, phones, emails, addresses,
average_rating, reviews_count, risk_level, risk_reasons, merged_sources,
manual_review_required, last_updated, created_at`

---

## GET /health

```json
{"status": "ok"}
```
