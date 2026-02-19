"""ReviewSearcher tests — DDG search + parser dispatch (all mocked)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.enrichment.review_searcher as rs_module
from src.collectors.base import RawReview
from src.enrichment.review_searcher import ReviewSearcher, _MAX_URLS_PER_PARSER


@pytest.fixture
def searcher() -> ReviewSearcher:
    return ReviewSearcher()


def _make_raw(name: str | None = "БурПро"):
    raw = MagicMock()
    raw.name_raw = name
    raw.id = "uuid-test-1234"
    return raw


# ---------------------------------------------------------------------------
# Guard conditions — return [] early
# ---------------------------------------------------------------------------


async def test_search_disabled_by_config(searcher, monkeypatch):
    monkeypatch.setattr("src.enrichment.review_searcher.settings.enable_review_enrichment", False)
    result = await searcher.search(_make_raw())
    assert result == []


async def test_search_empty_name_returns_empty(searcher, monkeypatch):
    monkeypatch.setattr("src.enrichment.review_searcher.settings.enable_review_enrichment", True)
    result = await searcher.search(_make_raw(name=""))
    assert result == []


async def test_search_none_name_returns_empty(searcher, monkeypatch):
    monkeypatch.setattr("src.enrichment.review_searcher.settings.enable_review_enrichment", True)
    result = await searcher.search(_make_raw(name=None))
    assert result == []


# ---------------------------------------------------------------------------
# URL classification — _classify_urls
# ---------------------------------------------------------------------------


def test_classify_urls_flamp(searcher):
    urls = ["https://omsk.flamp.ru/firm/burpro-123"]
    classified = searcher._classify_urls(urls)
    assert "flamp" in classified
    assert "https://omsk.flamp.ru/firm/burpro-123" in classified["flamp"]


def test_classify_urls_vk(searcher):
    urls = ["https://vk.com/burpro_omsk"]
    classified = searcher._classify_urls(urls)
    assert "vk" in classified
    assert "https://vk.com/burpro_omsk" in classified["vk"]


def test_classify_urls_otzovik(searcher):
    urls = ["https://otzovik.com/reviews/burpro/"]
    classified = searcher._classify_urls(urls)
    assert "otzovik" in classified


def test_classify_urls_unknown_skipped(searcher):
    urls = ["https://unknown-site.com/reviews/burpro/"]
    classified = searcher._classify_urls(urls)
    assert classified == {}


def test_classify_urls_deduplicates(searcher):
    """Same URL twice → parsed only once."""
    url = "https://omsk.flamp.ru/firm/burpro-123"
    classified = searcher._classify_urls([url, url])
    assert classified["flamp"].count(url) == 1


def test_classify_urls_caps_per_parser(searcher):
    """More than _MAX_URLS_PER_PARSER URLs for one parser → capped."""
    urls = [f"https://omsk.flamp.ru/firm/company-{i}" for i in range(10)]
    classified = searcher._classify_urls(urls)
    assert len(classified.get("flamp", [])) <= _MAX_URLS_PER_PARSER


def test_classify_urls_case_insensitive_dedup(searcher):
    """URLs differing only in case → deduplicated."""
    urls = [
        "https://omsk.flamp.ru/firm/burpro-123",
        "https://omsk.FLAMP.ru/firm/burpro-123",
    ]
    classified = searcher._classify_urls(urls)
    assert len(classified.get("flamp", [])) == 1


# ---------------------------------------------------------------------------
# _build_queries
# ---------------------------------------------------------------------------


def test_build_queries_contains_three_queries(searcher):
    queries = searcher._build_queries("БурПро")
    assert len(queries) == 3


def test_build_queries_contain_name(searcher):
    queries = searcher._build_queries("ООО БурПро")
    for q in queries:
        assert "БурПро" in q or "ООО" in q


# ---------------------------------------------------------------------------
# Full search flow — mocked _search_urls
# ---------------------------------------------------------------------------


async def test_search_dispatches_to_parsers(searcher, monkeypatch):
    """When _search_urls returns a flamp URL, FlampParser.safe_parse is called."""
    monkeypatch.setattr("src.enrichment.review_searcher.settings.enable_review_enrichment", True)

    flamp_url = "https://omsk.flamp.ru/firm/burpro-12345678"

    async def _mock_search_urls(query):
        return [flamp_url]

    monkeypatch.setattr(searcher, "_search_urls", _mock_search_urls)

    fake_review = RawReview(source="flamp", text="Отличная работа")
    flamp_parser = next(p for p in rs_module._PARSERS if p.source_name == "flamp")
    with patch.object(flamp_parser, "safe_parse", AsyncMock(return_value=[fake_review])):
        results = await searcher.search(_make_raw())

    assert any(rv.source == "flamp" for rv in results)


async def test_search_returns_empty_when_no_urls_found(searcher, monkeypatch):
    monkeypatch.setattr("src.enrichment.review_searcher.settings.enable_review_enrichment", True)

    async def _mock_search_urls(query):
        return []

    monkeypatch.setattr(searcher, "_search_urls", _mock_search_urls)

    results = await searcher.search(_make_raw())
    assert results == []
