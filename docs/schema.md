# Database Schema

## Overview

Четыре таблицы реализуют пайплайн обработки данных:

```
companies_raw_omsk  →  reviews_raw_omsk
        ↓
companies_enriched
        ↓
companies_omsk_clean   ← финальная таблица
```

---

## companies_raw_omsk

Сырые записи, собранные с площадок (Avito, 2GIS, Яндекс.Карты).

| Column | Type | Description |
|---|---|---|
| id | UUID PK | Первичный ключ |
| source | VARCHAR(50) | avito / 2gis / yandex |
| source_id | VARCHAR(255) | ID объявления/карточки на площадке |
| source_link | TEXT | URL страницы |
| raw_payload | JSONB | Snapshot HTML/JSON |
| name_raw | TEXT | Название как есть |
| phones | JSONB | Сырые телефоны |
| emails | JSONB | Email-адреса |
| addresses | JSONB | Сырые адреса |
| contacts_json | JSONB | Прочие контакты (сайты, соцсети, мессенджеры) |
| inn | VARCHAR(12) | ИНН (если найден) |
| ogrn | VARCHAR(15) | ОГРН |
| average_rating | FLOAT | Средний рейтинг |
| reviews_count | INT | Количество отзывов |
| collected_at | TIMESTAMPTZ | Время сбора |
| is_processed | BOOL | Прошёл обогащение? |

---

## reviews_raw_omsk

Отзывы из всех источников (коллекторы + обогащение: Flamp, VK, Отзовик).

| Column | Type | Description |
|---|---|---|
| id | UUID PK | |
| company_raw_id | UUID FK | → companies_raw_omsk.id |
| source | VARCHAR(50) | avito / 2gis / yandex / flamp / vk / otzovik |
| text | TEXT | Текст отзыва |
| rating | FLOAT | 1–5 |
| author | VARCHAR(255) | Имя автора |
| review_date | TIMESTAMPTZ | Дата отзыва |
| source_link | TEXT | URL отзыва |

---

## companies_enriched

Результат обогащения: нормализованные данные + проверки по 13 реестрам.

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
| checks | JSONB | Результаты 13 реестров (см. ниже) |
| confidence_score | INT | 0–100 (16 факторов) |
| manual_review_required | BOOL | true если < CONFIDENCE_THRESHOLD |
| enriched_at | TIMESTAMPTZ | |

### checks JSONB structure

```json
{
  "dadata_fns":       {"registry": "dadata_fns",       "found": true,  "status": "ACTIVE", "details": {...}},
  "fns_pb":           {"registry": "fns_pb",           "found": true,  "details": {"risk_markers": [], "head_fio": "..."}},
  "fssp":             {"registry": "fssp",             "found": false},
  "kad_arbitr":       {"registry": "kad_arbitr",       "found": true,  "status": "has_cases", "details": {...}},
  "efrsb":            {"registry": "efrsb",            "found": false},
  "eis_zakupki":      {"registry": "eis_zakupki",      "found": false},
  "nostroy":          {"registry": "nostroy",          "found": true,  "status": "active"},
  "fns_msp":          {"registry": "fns_msp",          "found": true,  "details": {"in_msp": true, "category": "micro"}},
  "rnp":              {"registry": "rnp",              "found": false},
  "proverki":         {"registry": "proverki",         "found": true,  "details": {"inspections_count": 2, "with_violations": 0}},
  "fns_npd":          {"registry": "fns_npd",          "found": false},
  "fns_disqualified": {"registry": "fns_disqualified", "found": false},
  "fns_mass_address": {"registry": "fns_mass_address", "found": false}
}
```

### Confidence score (16 факторов, макс. 100)

| Фактор | Вес |
|---|---|
| has_inn | 15 |
| has_ogrn | 8 |
| has_phone | 5 |
| has_address | 2 |
| dadata_found | 10 |
| dadata_active | 15 |
| fssp_clean | 5 |
| nostroy_active | 8 |
| rnp_clean | 7 |
| msp_found | 3 |
| disqualification_clean | 5 |
| mass_address_clean | 4 |
| efrsb_clean | 3 |
| fns_pb_no_risks | 5 |
| proverki_clean | 2 |
| npd_confirmed | 2 |

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
| contacts_json | JSONB | Сайты, соцсети, мессенджеры |
| average_rating | FLOAT | |
| reviews_count | INT | |
| reviews_sample | JSONB | До 200 отзывов |
| summary_review | TEXT | AI-резюме (GPT-4o-mini) |
| risk_level | VARCHAR(10) | green / yellow / red |
| risk_reasons | JSONB | До 7 пунктов |
| source_records | JSONB | UUID-список raw-записей |
| merged_sources | JSONB | ['avito', '2gis', 'yandex'] |
| checks | JSONB | Реестровые проверки (13 шт.) |
| manual_review_required | BOOL | |
| last_updated | TIMESTAMPTZ | |
| created_at | TIMESTAMPTZ | |
