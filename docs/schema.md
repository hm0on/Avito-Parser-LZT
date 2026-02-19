# Database Schema

## Overview

Four tables implement the data pipeline:

```
companies_raw_omsk  →  reviews_raw_omsk
        ↓
companies_enriched
        ↓
companies_omsk_clean   ← final output
```

---

## companies_raw_omsk

Сырые записи, собранные с площадок.

| Column | Type | Description |
|---|---|---|
| id | UUID PK | Первичный ключ |
| source | VARCHAR(50) | avito / 2gis / yandex / vk / flamp |
| source_id | VARCHAR(255) | ID объявления/карточки на площадке |
| source_link | TEXT | URL страницы |
| raw_payload | JSONB | Snapshot HTML/JSON |
| name_raw | TEXT | Название как есть |
| phones | JSONB | Сырые телефоны |
| emails | JSONB | Email-адреса |
| addresses | JSONB | Сырые адреса |
| contacts_json | JSONB | Прочие контакты |
| inn | VARCHAR(12) | ИНН (если найден) |
| ogrn | VARCHAR(15) | ОГРН |
| average_rating | FLOAT | Средний рейтинг |
| reviews_count | INT | Количество отзывов |
| collected_at | TIMESTAMPTZ | Время сбора |
| is_processed | BOOL | Прошёл обогащение? |

---

## reviews_raw_omsk

| Column | Type | Description |
|---|---|---|
| id | UUID PK | |
| company_raw_id | UUID FK | → companies_raw_omsk.id |
| source | VARCHAR(50) | Источник |
| text | TEXT | Текст отзыва |
| rating | FLOAT | 1–5 |
| author | VARCHAR(255) | Имя автора |
| review_date | TIMESTAMPTZ | Дата отзыва |
| source_link | TEXT | URL отзыва |

---

## companies_enriched

| Column | Type | Description |
|---|---|---|
| id | UUID PK | |
| raw_id | UUID FK (unique) | → companies_raw_omsk.id |
| inn | VARCHAR(12) | Нормализованный ИНН |
| ogrn | VARCHAR(15) | |
| name_normalized | TEXT | Название из ФНС / DaData |
| entity_type | VARCHAR(20) | ЮЛ / ИП / ФЛ / unknown |
| phones_normalized | JSONB | E.164 телефоны |
| addresses_parsed | JSONB | Разобранные адреса |
| checks | JSONB | Результаты реестров |
| confidence_score | INT | 0–100 |
| manual_review_required | BOOL | < CONFIDENCE_THRESHOLD |
| enriched_at | TIMESTAMPTZ | |

### checks JSONB structure

```json
{
  "dadata_fns": {"registry": "dadata_fns", "found": true, "status": "ACTIVE", "details": {...}},
  "fssp":       {"registry": "fssp",       "found": false},
  "kad_arbitr": {"registry": "kad_arbitr", "found": true, "status": "has_cases", "details": {...}},
  "efrsb":      {"registry": "efrsb",      "found": false},
  "eis_zakupki":{"registry": "eis_zakupki","found": false},
  "nostroy":    {"registry": "nostroy",    "found": true,  "status": "active", "details": {...}}
}
```

---

## companies_omsk_clean

Финальная таблица. Обновляется еженедельно.

| Column | Type | Description |
|---|---|---|
| id | UUID PK | |
| inn | VARCHAR(12) | |
| ogrn | VARCHAR(15) | |
| name_normalized | TEXT | |
| entity_type | VARCHAR(20) | ЮЛ / ИП / ФЛ / unknown |
| phones | JSONB | E.164 |
| emails | JSONB | |
| addresses | JSONB | |
| contacts_json | JSONB | |
| average_rating | FLOAT | |
| reviews_count | INT | |
| reviews_sample | JSONB | До 200 отзывов |
| summary_review | TEXT | AI резюме |
| risk_level | VARCHAR(10) | green / yellow / red |
| risk_reasons | JSONB | max 5 пунктов |
| source_records | JSONB | raw_id UUID list |
| merged_sources | JSONB | ['avito', '2gis'] |
| checks | JSONB | Реестровые проверки |
| manual_review_required | BOOL | |
| last_updated | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |
