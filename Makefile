.PHONY: \
	up down build verify-collector-image logs logs-api logs-scheduler logs-collector \
	migrate migrate-create migrate-down shell-db db-check \
	run-pipeline run-avito run-yandex run-twogis run-enrichment \
	run-pipeline-detached run-enrichment-detached \
	run-smoke-60-fast run-smoke-60-fast-detached \
	run-smoke-60 run-smoke-60-detached \
	run-smoke-90 run-smoke-90-detached \
	install lint test export-csv env

# ── Docker ─────────────────────────────────────────────────────────────────
up:
	docker compose up -d db api scheduler

down:
	docker compose down

build:
	COMPOSE_PROFILES=manual docker compose build api scheduler collector

verify-collector-image:
	COMPOSE_PROFILES=manual docker compose run --rm collector python -c "from src.config import settings; import src.enrichment.registries.listorg as listorg; import src.enrichment.review_parsers.flamp as flamp; assert hasattr(settings, 'avito_light_enrichment'); assert hasattr(settings, 'summarizer_workers'); assert hasattr(settings, 'summarizer_max_input_reviews'); assert hasattr(listorg, '_extract_founders'); assert hasattr(flamp, 'normalize_flamp_url'); print('collector image OK')"

logs:
	docker compose logs -f --tail=200

logs-api:
	docker compose logs -f --tail=100 api

logs-scheduler:
	docker compose logs -f --tail=100 scheduler

logs-collector:
	docker compose logs -f --tail=200 collector

# ── Database ────────────────────────────────────────────────────────────────
migrate:
	docker compose run --rm api alembic upgrade head

migrate-create:
	docker compose run --rm api alembic revision --autogenerate -m "$(msg)"

migrate-down:
	docker compose run --rm api alembic downgrade -1

shell-db:
	docker compose exec db psql -U $${POSTGRES_USER:-avito} -d $${POSTGRES_DB:-avito_parser}

# Verify DB tables and checkpoint statuses
db-check:
	@echo "=== Collection checkpoints ==="
	docker compose exec db psql -U $${POSTGRES_USER:-avito} -d $${POSTGRES_DB:-avito_parser} \
		-c "SELECT source, week_start, status, companies_count, reviews_count, started_at, completed_at FROM collection_checkpoints ORDER BY week_start DESC, source;"
	@echo ""
	@echo "=== Table row counts ==="
	docker compose exec db psql -U $${POSTGRES_USER:-avito} -d $${POSTGRES_DB:-avito_parser} \
		-c "SELECT 'companies_raw_omsk' AS tbl, COUNT(*) FROM companies_raw_omsk UNION ALL SELECT 'reviews_raw_omsk', COUNT(*) FROM reviews_raw_omsk UNION ALL SELECT 'companies_enriched', COUNT(*) FROM companies_enriched UNION ALL SELECT 'companies_omsk_clean', COUNT(*) FROM companies_omsk_clean UNION ALL SELECT 'collection_checkpoints', COUNT(*) FROM collection_checkpoints ORDER BY tbl;"

# ── Pipeline (interactive, blocks terminal) ─────────────────────────────────
run-pipeline:
	COMPOSE_PROFILES=manual docker compose run --rm collector python -m src.pipeline.runner

run-smoke-90:
	COMPOSE_PROFILES=manual docker compose run --rm collector python -c "import asyncio; from src.pipeline.runner import PipelineRunner; asyncio.run(PipelineRunner(company_limit=90).run())"

run-smoke-60:
	COMPOSE_PROFILES=manual docker compose run --rm collector python -c "import asyncio; from src.pipeline.runner import PipelineRunner; asyncio.run(PipelineRunner(company_limit=60).run())"

run-smoke-60-fast:
	PIPELINE_SOURCE_PARALLELISM=3 ENRICHMENT_WORKERS=160 AI_WORKERS=160 AVITO_REVIEW_WORKERS=32 TWOGIS_FIRM_WORKERS=32 TWOGIS_TABS_PER_BROWSER=8 COMPOSE_PROFILES=manual docker compose run --rm collector python -c "import asyncio; from src.pipeline.runner import PipelineRunner; asyncio.run(PipelineRunner(company_limit=60).run())"

run-avito:
	COMPOSE_PROFILES=manual docker compose run --rm collector python -m src.pipeline.avito_runner

run-yandex:
	COMPOSE_PROFILES=manual docker compose run --rm collector python -m src.pipeline.yandex_runner

run-twogis:
	COMPOSE_PROFILES=manual docker compose run --rm collector python -m src.pipeline.twogis_runner

run-enrichment:
	COMPOSE_PROFILES=manual docker compose run --rm collector python -m src.pipeline.enrichment_runner

# ── Pipeline (detached — survives SSH disconnect) ───────────────────────────
run-pipeline-detached:
	COMPOSE_PROFILES=manual docker compose run -d --name pipeline-run collector python -m src.pipeline.runner
	@echo "Pipeline started in background. Container: pipeline-run"
	@echo "  Logs:  docker logs -f pipeline-run"
	@echo "  Stop:  docker stop pipeline-run && docker rm pipeline-run"

run-smoke-90-detached:
	COMPOSE_PROFILES=manual docker compose run -d --name pipeline-smoke-90 collector python -c "import asyncio; from src.pipeline.runner import PipelineRunner; asyncio.run(PipelineRunner(company_limit=90).run())"
	@echo "Smoke pipeline started in background. Container: pipeline-smoke-90"
	@echo "  Logs:  docker logs -f pipeline-smoke-90"
	@echo "  Stop:  docker stop pipeline-smoke-90 && docker rm pipeline-smoke-90"

run-smoke-60-detached:
	COMPOSE_PROFILES=manual docker compose run -d --name pipeline-smoke-60 collector python -c "import asyncio; from src.pipeline.runner import PipelineRunner; asyncio.run(PipelineRunner(company_limit=60).run())"
	@echo "Smoke pipeline started in background. Container: pipeline-smoke-60"
	@echo "  Logs:  docker logs -f pipeline-smoke-60"
	@echo "  Stop:  docker stop pipeline-smoke-60 && docker rm pipeline-smoke-60"

run-smoke-60-fast-detached:
	PIPELINE_SOURCE_PARALLELISM=3 ENRICHMENT_WORKERS=160 AI_WORKERS=160 AVITO_REVIEW_WORKERS=32 TWOGIS_FIRM_WORKERS=32 TWOGIS_TABS_PER_BROWSER=8 COMPOSE_PROFILES=manual docker compose run -d --name pipeline-smoke-60 collector python -c "import asyncio; from src.pipeline.runner import PipelineRunner; asyncio.run(PipelineRunner(company_limit=60).run())"
	@echo "Fast smoke pipeline started in background. Container: pipeline-smoke-60"
	@echo "  Logs:  docker logs -f pipeline-smoke-60"
	@echo "  Stop:  docker stop pipeline-smoke-60 && docker rm pipeline-smoke-60"

run-enrichment-detached:
	COMPOSE_PROFILES=manual docker compose run -d --name enrichment-run collector python -m src.pipeline.enrichment_runner
	@echo "Enrichment started in background. Container: enrichment-run"
	@echo "  Logs:  docker logs -f enrichment-run"
	@echo "  Stop:  docker stop enrichment-run && docker rm enrichment-run"

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
	@echo ".env created — fill in your API keys and proxy settings"
