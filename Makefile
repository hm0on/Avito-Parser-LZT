.PHONY: up down build migrate run-pipeline shell-db lint test

# ── Docker ─────────────────────────────────────────────────────────────────
up:
	docker compose up -d db api scheduler

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

# ── Database ────────────────────────────────────────────────────────────────
migrate:
	docker compose run --rm api alembic upgrade head

migrate-create:
	docker compose run --rm api alembic revision --autogenerate -m "$(msg)"

migrate-down:
	docker compose run --rm api alembic downgrade -1

shell-db:
	docker compose exec db psql -U $${POSTGRES_USER:-avito} -d $${POSTGRES_DB:-avito_parser}

# ── Pipeline ────────────────────────────────────────────────────────────────
run-pipeline:
	docker compose run --rm collector python -m src.pipeline.runner

run-collector-avito:
	docker compose run --rm collector python -c "import asyncio; from src.collectors.avito import AvitoCollector; asyncio.run(AvitoCollector().run())"

run-collector-twogis:
	docker compose run --rm collector python -c "import asyncio; from src.collectors.twogis import TwoGisCollector; asyncio.run(TwoGisCollector().run())"

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
