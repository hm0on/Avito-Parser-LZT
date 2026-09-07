# Database Schema

## Overview

Текущий pipeline хранит данные в 5 таблицах:

```text
companies_raw_omsk  ->  reviews_raw_omsk
        |
        v
companies_enriched
        |
        v
companies_omsk_clean

collection_checkpoints (статусы этапов по неделям)
```

---

## companies_raw_omsk

Сырые записи, собранные source-раннерами.

| Column | Type | Description |
|---|---|---|
| id | UUID PK | Первичный ключ |
| source | VARCHAR(50) | `avito` / `2gis` / `yandex` |
| source_id | VARCHAR(255) | ID карточки/объявления на площадке |
| source_link | TEXT | URL исходной карточки |
| raw_payload | JSONB | Snapshot данных источника |
| name_raw | TEXT | Название как на источнике |
| phones | JSONB | Сырые телефоны |
| emails | JSONB | Email-адреса |
| addresses | JSONB | Сырые адреса |
| contacts_json | JSONB | Прочие контакты/мета |
| inn | VARCHAR(12) | ИНН (если найден) |
| ogrn | VARCHAR(15) | ОГРН/ОГРНИП |
| average_rating | FLOAT | Рейтинг источника |
| reviews_count | INT | Кол-во отзывов источника |
| collection_week_start | DATE | Неделя source-сбора |
| collected_at | TIMESTAMPTZ | Время сбора |
| is_processed | BOOL | Прошел enrichment |

---

## reviews_raw_omsk

Сырые отзывы (stage 1 + stage 2 enrichment).

| Column | Type | Description |
|---|---|---|
| id | UUID PK | |
| company_raw_id | UUID FK | -> `companies_raw_omsk.id` |
| source | VARCHAR(50) | `avito` / `yandex` / `2gis` / `flamp` / `vk` / `otzovik` |
| text | TEXT | Текст отзыва |
| rating | FLOAT | Оценка |
| author | VARCHAR(255) | Автор |
| review_date | TIMESTAMPTZ | Дата отзыва |
| source_link | TEXT | URL отзыва/карточки |

---

## companies_enriched

Промежуточные результаты enrichment по каждой raw-записи.

| Column | Type | Description |
|---|---|---|
| id | UUID PK | |
| raw_id | UUID FK UNIQUE | -> `companies_raw_omsk.id` |
| inn | VARCHAR(12) | Нормализованный ИНН |
| ogrn | VARCHAR(15) | Нормализованный ОГРН |
| name_normalized | TEXT | Нормализованное название |
| entity_type | VARCHAR(20) | `ЮЛ` / `ИП` / `unknown` |
| phones_normalized | JSONB | Нормализованные телефоны |
| addresses_parsed | JSONB | Парс адресов |
| checks | JSONB | Результаты проверок реестров |
| confidence_score | INT | 0..100 |
| manual_review_required | BOOL | Флаг ручной проверки |
| pipeline_week_start | DATE | Неделя pipeline (enrichment) |
| enriched_at | TIMESTAMPTZ | Время обогащения |

---

## companies_omsk_clean

Финальная таблица API.

| Column | Type | Description |
|---|---|---|
| id | UUID PK | |
| inn | VARCHAR(12) | |
| ogrn | VARCHAR(15) | |
| name_normalized | TEXT | Финальное имя |
| entity_type | VARCHAR(20) | `ЮЛ` / `ИП` / `unknown` |
| phones | JSONB | Финальные телефоны |
| emails | JSONB | Финальные email |
| addresses | JSONB | Финальные адреса |
| contacts_json | JSONB | Контакты и мета |
| average_rating | FLOAT | Пересчитанный рейтинг |
| reviews_count | INT | Фактически собранные отзывы |
| reviews_sample | JSONB | Сэмпл отзывов (до лимита) |
| summary_review | TEXT | AI summary |
| risk_level | VARCHAR(10) | `green` / `yellow` / `red` |
| risk_reasons | JSONB | Причины риска |
| source_records | JSONB | Список `raw_id` объединенных записей |
| merged_sources | JSONB | Список источников (`avito`,`yandex`,`2gis`) |
| source_links | JSONB | Ссылки по каждому источнику |
| checks | JSONB | Итоговые реестровые проверки |
| manual_review_required | BOOL | Флаг ручной проверки |
| pipeline_week_start | DATE | Неделя pipeline |
| last_updated | TIMESTAMPTZ | Обновлено |
| created_at | TIMESTAMPTZ | Создано |

---

## collection_checkpoints

Статусы этапов пайплайна по каждой неделе.

| Column | Type | Description |
|---|---|---|
| id | UUID PK | |
| source | VARCHAR(50) | `avito` / `yandex` / `2gis` / `enrichment` |
| week_start | DATE | Начало недели |
| status | VARCHAR(20) | `pending` / `running` / `completed` / `failed` |
| started_at | TIMESTAMPTZ | Время старта этапа |
| completed_at | TIMESTAMPTZ | Время завершения этапа |
| companies_count | INT | Количество компаний на этапе |
| reviews_count | INT | Количество отзывов на этапе |
| error_text | TEXT | Текст ошибки |
| details_json | JSONB | Доп. мета этапа |
| updated_at | TIMESTAMPTZ | Последнее обновление |

Constraint:
- `UNIQUE(source, week_start)`

---

## Weekly Semantics

- Source-раннеры (`avito`, `yandex`, `2gis`) пишут только `companies_raw_omsk`/`reviews_raw_omsk` своей недели.
- Enrichment запускается только когда все обязательные source за неделю имеют `completed` и `companies_count > 0`.
- Перед новым enrichment за ту же неделю очищаются только enrichment-результаты этой недели (`companies_enriched`, `companies_omsk_clean`, stage2 reviews).
- Исторические недели остаются в БД и доступны через API-фильтр `week_start`.
