import pytest

import src.pipeline.source_runner as source_runner_module
from src.collectors.avito_proxy_precheck import ProxyPoolExhaustedError
from src.collectors.base import AbstractCollector, RawCompany
from src.collectors.twogis import TwoGisProxyAuthError
from src.pipeline.source_runner import SourceCollectorRunner


class _DummySession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def commit(self):
        return None


class _FakeCollector(AbstractCollector):
    source_name = "fake"

    def __init__(self, batches: dict[str, list[RawCompany]]) -> None:
        self._batches = batches

    async def collect(self, keyword: str) -> list[RawCompany]:
        return list(self._batches.get(keyword, []))


@pytest.mark.asyncio
async def test_source_runner_persists_batches_progressively(monkeypatch):
    batches = {
        "kw1": [
            RawCompany(source="fake", name_raw="A", reviews_count=2),
            RawCompany(source="fake", name_raw="B", reviews_count=1),
        ],
        "kw2": [
            RawCompany(source="fake", name_raw="C", reviews_count=3),
        ],
    }
    collector = _FakeCollector(batches)
    runner = SourceCollectorRunner(collector, company_limit=0)

    checkpoint_calls: list[dict] = []
    append_calls: list[list[str]] = []

    monkeypatch.setattr(source_runner_module.settings, "search_plumbing_keywords", "kw1,kw2")
    monkeypatch.setattr(source_runner_module, "AsyncSessionLocal", _DummySession)

    async def fake_clear_source_collection(session, *, source, week_start):  # noqa: ARG001
        return None

    async def fake_reset_enrichment_checkpoint(session, *, week_start, reason):  # noqa: ARG001
        return None

    async def fake_append_source_collection(session, *, week_start, raw_companies):  # noqa: ARG001
        append_calls.append([company.name_raw or "" for company in raw_companies])
        return {
            "companies_count": len(raw_companies),
            "reviews_count": sum(company.reviews_count or 0 for company in raw_companies),
        }

    async def fake_mark_checkpoint(
        session,  # noqa: ARG001
        *,
        source,
        week_start,
        status,
        companies_count=None,
        reviews_count=None,
        error_text=None,
        details_json=None,
    ):
        checkpoint_calls.append(
            {
                "source": source,
                "status": status,
                "companies_count": companies_count,
                "reviews_count": reviews_count,
                "error_text": error_text,
                "details_json": details_json,
            }
        )
        return None

    monkeypatch.setattr(source_runner_module, "clear_source_collection", fake_clear_source_collection)
    monkeypatch.setattr(
        source_runner_module,
        "reset_enrichment_checkpoint",
        fake_reset_enrichment_checkpoint,
    )
    monkeypatch.setattr(source_runner_module, "append_source_collection", fake_append_source_collection)
    monkeypatch.setattr(source_runner_module, "mark_checkpoint", fake_mark_checkpoint)
    async def fake_apply_relevance_filter(batch, *, keyword):  # noqa: ARG001
        return batch, {
            "filtered_domain_out": 0,
            "filtered_geo_out": 0,
            "filtered_low_confidence_out": 0,
        }

    monkeypatch.setattr(runner, "_apply_relevance_filter", fake_apply_relevance_filter)

    stats = await runner.run()

    assert stats["companies_count"] == 3
    assert stats["reviews_count"] == 6
    assert append_calls == [["A", "B"], ["C"]]

    running_updates = [call for call in checkpoint_calls if call["status"] == "running"]
    assert running_updates[0]["companies_count"] == 0
    assert running_updates[1]["companies_count"] == 2
    assert running_updates[1]["reviews_count"] == 3
    assert running_updates[2]["companies_count"] == 3
    assert running_updates[2]["reviews_count"] == 6

    completed = [call for call in checkpoint_calls if call["status"] == "completed"]
    assert len(completed) == 1
    assert completed[0]["companies_count"] == 3
    assert completed[0]["reviews_count"] == 6


@pytest.mark.asyncio
async def test_source_runner_fails_when_no_companies_collected(monkeypatch):
    collector = _FakeCollector({"kw1": []})
    runner = SourceCollectorRunner(collector, company_limit=0)

    checkpoint_calls: list[dict] = []

    monkeypatch.setattr(source_runner_module.settings, "search_plumbing_keywords", "kw1")
    monkeypatch.setattr(source_runner_module, "AsyncSessionLocal", _DummySession)

    async def fake_clear_source_collection(session, *, source, week_start):  # noqa: ARG001
        return None

    async def fake_reset_enrichment_checkpoint(session, *, week_start, reason):  # noqa: ARG001
        return None

    async def fake_append_source_collection(session, *, week_start, raw_companies):  # noqa: ARG001
        return {"companies_count": 0, "reviews_count": 0}

    async def fake_mark_checkpoint(
        session,  # noqa: ARG001
        *,
        source,
        week_start,
        status,
        companies_count=None,
        reviews_count=None,
        error_text=None,
        details_json=None,
    ):
        checkpoint_calls.append(
            {
                "source": source,
                "status": status,
                "companies_count": companies_count,
                "reviews_count": reviews_count,
                "error_text": error_text,
                "details_json": details_json,
            }
        )
        return None

    monkeypatch.setattr(source_runner_module, "clear_source_collection", fake_clear_source_collection)
    monkeypatch.setattr(
        source_runner_module,
        "reset_enrichment_checkpoint",
        fake_reset_enrichment_checkpoint,
    )
    monkeypatch.setattr(source_runner_module, "append_source_collection", fake_append_source_collection)
    monkeypatch.setattr(source_runner_module, "mark_checkpoint", fake_mark_checkpoint)
    async def fake_apply_relevance_filter(batch, *, keyword):  # noqa: ARG001
        return batch, {
            "filtered_domain_out": 0,
            "filtered_geo_out": 0,
            "filtered_low_confidence_out": 0,
        }

    monkeypatch.setattr(runner, "_apply_relevance_filter", fake_apply_relevance_filter)

    with pytest.raises(RuntimeError, match="collected 0 companies"):
        await runner.run()

    assert checkpoint_calls[0]["status"] == "running"
    assert checkpoint_calls[-1]["status"] == "failed"


class _PrecheckFailCollector(AbstractCollector):
    source_name = "fake_precheck"

    async def collect(self, keyword: str) -> list[RawCompany]:
        return []

    async def precheck_proxies(self):
        raise ProxyPoolExhaustedError("Only 0 usable proxies (need 1)")


@pytest.mark.asyncio
async def test_source_runner_fails_fast_on_proxy_exhausted(monkeypatch):
    collector = _PrecheckFailCollector()
    runner = SourceCollectorRunner(collector, company_limit=0)

    checkpoint_calls: list[dict] = []

    monkeypatch.setattr(source_runner_module.settings, "search_plumbing_keywords", "kw1")
    monkeypatch.setattr(source_runner_module, "AsyncSessionLocal", _DummySession)

    async def fake_clear_source_collection(session, *, source, week_start):  # noqa: ARG001
        return None

    async def fake_reset_enrichment_checkpoint(session, *, week_start, reason):  # noqa: ARG001
        return None

    async def fake_append_source_collection(session, *, week_start, raw_companies):  # noqa: ARG001
        return {"companies_count": 0, "reviews_count": 0}

    async def fake_mark_checkpoint(
        session,  # noqa: ARG001
        *,
        source,
        week_start,
        status,
        companies_count=None,
        reviews_count=None,
        error_text=None,
        details_json=None,
    ):
        checkpoint_calls.append({"source": source, "status": status, "error_text": error_text})
        return None

    monkeypatch.setattr(source_runner_module, "clear_source_collection", fake_clear_source_collection)
    monkeypatch.setattr(source_runner_module, "reset_enrichment_checkpoint", fake_reset_enrichment_checkpoint)
    monkeypatch.setattr(source_runner_module, "append_source_collection", fake_append_source_collection)
    monkeypatch.setattr(source_runner_module, "mark_checkpoint", fake_mark_checkpoint)

    with pytest.raises(RuntimeError, match="Proxy pool unusable"):
        await runner.run()

    assert checkpoint_calls[0]["status"] == "running"
    assert checkpoint_calls[-1]["status"] == "failed"
    assert "Proxy pool unusable" in (checkpoint_calls[-1]["error_text"] or "")


class _TwoGisProxyAuthFailCollector(AbstractCollector):
    source_name = "2gis"

    async def collect(self, keyword: str) -> list[RawCompany]:
        raise TwoGisProxyAuthError(
            "2GIS proxy pool unauthorized: 3/3 attempts failed with proxy auth errors"
        )


@pytest.mark.asyncio
async def test_source_runner_fails_with_details_on_twogis_proxy_auth(monkeypatch):
    """TwoGisProxyAuthError during collection results in 'failed' checkpoint with details_json."""
    collector = _TwoGisProxyAuthFailCollector()
    runner = SourceCollectorRunner(collector, company_limit=0)

    checkpoint_calls: list[dict] = []

    monkeypatch.setattr(source_runner_module.settings, "search_plumbing_keywords", "kw1")
    monkeypatch.setattr(source_runner_module, "AsyncSessionLocal", _DummySession)

    async def fake_clear_source_collection(session, *, source, week_start):  # noqa: ARG001
        return None

    async def fake_reset_enrichment_checkpoint(session, *, week_start, reason):  # noqa: ARG001
        return None

    async def fake_append_source_collection(session, *, week_start, raw_companies):  # noqa: ARG001
        return {"companies_count": 0, "reviews_count": 0}

    async def fake_mark_checkpoint(
        session,  # noqa: ARG001
        *,
        source,
        week_start,
        status,
        companies_count=None,
        reviews_count=None,
        error_text=None,
        details_json=None,
    ):
        checkpoint_calls.append({
            "source": source,
            "status": status,
            "error_text": error_text,
            "details_json": details_json,
        })
        return None

    monkeypatch.setattr(source_runner_module, "clear_source_collection", fake_clear_source_collection)
    monkeypatch.setattr(source_runner_module, "reset_enrichment_checkpoint", fake_reset_enrichment_checkpoint)
    monkeypatch.setattr(source_runner_module, "append_source_collection", fake_append_source_collection)
    monkeypatch.setattr(source_runner_module, "mark_checkpoint", fake_mark_checkpoint)

    with pytest.raises(TwoGisProxyAuthError, match="proxy pool unauthorized"):
        await runner.run()

    assert checkpoint_calls[-1]["status"] == "failed"
    assert "proxy pool unauthorized" in (checkpoint_calls[-1]["error_text"] or "").lower()
    assert checkpoint_calls[-1]["details_json"] is not None
    assert checkpoint_calls[-1]["details_json"]["failure_type"] == "proxy_auth_exhausted"
