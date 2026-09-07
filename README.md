# Avito Parser

Сервис собирает компании и отзывы из Avito, Yandex Maps и 2GIS, обогащает данные внешними реестрами и предоставляет результаты через FastAPI.

## Быстрый старт

Требования: Docker Compose или Python 3.11+ для локального запуска тестов.

```bash
cp .env.example .env
# Заполните .env ключами и настройками прокси при необходимости.
mkdir -p storage/proxies
docker compose up -d db
make migrate
docker compose up -d api scheduler
```

Проверка: `curl http://localhost:8000/health`. Документация API доступна на `http://localhost:8000/docs`.

## Основные команды

- `make build` — собрать Docker-образы
- `make test` — запустить тесты
- `make lint` — проверить код
- `make run-pipeline` — запустить полный pipeline вручную
- `make run-avito`, `make run-yandex`, `make run-twogis` — запустить отдельный источник
- `make run-enrichment` — обогатить уже собранные данные

Подробности: [docs/runbook.md](docs/runbook.md), [docs/project_structure.md](docs/project_structure.md), [docs/api.md](docs/api.md), [docs/api_keys.md](docs/api_keys.md).

## Конфигурация и безопасность

Все API-ключи, пароли БД и прокси задаются только через `.env` или локальные файлы в `storage/`. Эти пути добавлены в `.gitignore`; реальные значения нельзя коммитить. Файл `proxies.txt.example` содержит только шаблон.

Перед публикацией репозитория проверяйте секреты специализированным сканером и отзывайте уже скомпрометированные ключи у провайдеров.

## Архитектура

Поток данных: `collectors` → `pipeline/enrichment` → PostgreSQL → FastAPI. Схема базы управляется Alembic, еженедельный запуск выполняет `scheduler`.

## Лицензия

Лицензия пока не выбрана.
