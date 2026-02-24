.PHONY: up down build migrate run-pipeline shell-db lint test

COMPOSE := docker compose
DEV_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.dev.yml

# ── Docker ─────────────────────────────────────────────────────────────────
up:
	$(DEV_COMPOSE) up -d db api scheduler

down:
	$(DEV_COMPOSE) down

build:
	$(DEV_COMPOSE) build

logs:
	$(DEV_COMPOSE) logs -f

# ── Database ────────────────────────────────────────────────────────────────
migrate:
	$(DEV_COMPOSE) run --rm api alembic upgrade head

migrate-create:
	$(DEV_COMPOSE) run --rm api alembic revision --autogenerate -m "$(msg)"

migrate-down:
	$(DEV_COMPOSE) run --rm api alembic downgrade -1

shell-db:
	$(DEV_COMPOSE) exec db psql -U $${POSTGRES_USER:-avito} -d $${POSTGRES_DB:-avito_parser}

# ── Pipeline ────────────────────────────────────────────────────────────────
run-pipeline:
	$(DEV_COMPOSE) run --rm collector python -m src.pipeline.runner

run-collector-avito:
	$(DEV_COMPOSE) run --rm collector python -c "import asyncio; from src.collectors.avito import AvitoCollector; asyncio.run(AvitoCollector().run())"

run-collector-twogis:
	$(DEV_COMPOSE) run --rm collector python -c "import asyncio; from src.collectors.twogis import TwoGisCollector; asyncio.run(TwoGisCollector().run())"

# ── Dev ─────────────────────────────────────────────────────────────────────
install:
	pip install -r requirements.txt
	playwright install chromium

lint:
	ruff check src/ scheduler/
	mypy src/ --ignore-missing-imports

test:
	pytest tests/ -v

# ── Exports ─────────────────────────────────────────────────────────────────
export-csv:
	curl -s "http://localhost:8000/exports?format=csv" -o companies_export.csv
	@echo "Saved to companies_export.csv"

# ── Env ──────────────────────────────────────────────────────────────────────
env:
	cp .env.example .env
	@echo ".env created — fill in your API keys"
