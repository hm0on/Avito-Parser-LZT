from __future__ import annotations

from src.pipeline.runner import PipelineRunner


def test_pipeline_runner_splits_smoke_limit_evenly():
    runner = PipelineRunner(company_limit=90)

    assert runner.company_limit == 90
    assert runner.per_source_limit == 30
    assert runner.avito_runner.company_limit == 30
    assert runner.twogis_runner.company_limit == 30
    assert runner.yandex_runner.company_limit == 30


def test_pipeline_runner_splits_sixty_into_twenty_per_source():
    runner = PipelineRunner(company_limit=60)

    assert runner.company_limit == 60
    assert runner.per_source_limit == 20
    assert runner.avito_runner.company_limit == 20
    assert runner.twogis_runner.company_limit == 20
    assert runner.yandex_runner.company_limit == 20


def test_pipeline_runner_without_limit_keeps_unbounded_sources():
    runner = PipelineRunner(company_limit=0)

    assert runner.company_limit == 0
    assert runner.per_source_limit == 0
    assert runner.avito_runner.company_limit == 0
    assert runner.twogis_runner.company_limit == 0
    assert runner.yandex_runner.company_limit == 0
