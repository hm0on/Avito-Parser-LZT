"""ReviewSearcher tests — DDG search + Flamp direct + parser dispatch (all mocked)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.enrichment.review_searcher as rs_module
from src.collectors.base import RawReview
from src.enrichment.review_searcher import (
    ReviewSearcher,
    _MAX_URLS_PER_PARSER,
    _build_flamp_queries,
    _clean_company_name,
)


@pytest.fixture
def searcher() -> ReviewSearcher:
    return ReviewSearcher()


def _make_raw(name: str | None = "БурПро", source: str = "avito", source_id: str | None = None):
    raw = MagicMock()
    raw.name_raw = name
    raw.id = "uuid-test-1234"
    raw.source = source
    raw.source_id = source_id
    raw.phones = []
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


def test_build_queries_contains_base_queries(searcher):
    queries = searcher._build_queries("БурПро")
    assert len(queries) == 2  # Base queries without phones


def test_build_queries_with_phone_adds_phone_queries(searcher, monkeypatch):
    monkeypatch.setattr("src.enrichment.review_searcher.settings.review_search_max_queries", 4)
    queries = searcher._build_queries("БурПро", phones=["+79991234567"])
    assert len(queries) == 4  # 2 base + 2 phone queries
    assert any("+79991234567" in q for q in queries)


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

    async def _mock_flamp_direct(name, phones=None):
        return []

    monkeypatch.setattr(searcher, "_search_urls", _mock_search_urls)
    monkeypatch.setattr(searcher, "_search_flamp_direct", _mock_flamp_direct)

    fake_review = RawReview(source="flamp", text="Отличная работа")
    flamp_parser = next(p for p in rs_module._PARSERS if p.source_name == "flamp")
    with patch.object(flamp_parser, "safe_parse", AsyncMock(return_value=[fake_review])):
        results = await searcher.search(_make_raw())

    assert any(rv.source == "flamp" for rv in results)


async def test_search_returns_empty_when_no_urls_found(searcher, monkeypatch):
    monkeypatch.setattr("src.enrichment.review_searcher.settings.enable_review_enrichment", True)

    async def _mock_search_urls(query):
        return []

    async def _mock_flamp_direct(name, phones=None):
        return []

    monkeypatch.setattr(searcher, "_search_urls", _mock_search_urls)
    monkeypatch.setattr(searcher, "_search_flamp_direct", _mock_flamp_direct)

    results = await searcher.search(_make_raw())
    assert results == []


# ---------------------------------------------------------------------------
# Direct Flamp search — _search_flamp_direct
# ---------------------------------------------------------------------------


async def test_search_2gis_constructs_flamp_url(searcher, monkeypatch):
    """For 2GIS companies, a Flamp URL should be constructed from source_id."""
    monkeypatch.setattr("src.enrichment.review_searcher.settings.enable_review_enrichment", True)

    async def _mock_search_urls(query):
        return []

    async def _mock_flamp_direct(name, phones=None):
        return []

    monkeypatch.setattr(searcher, "_search_urls", _mock_search_urls)
    monkeypatch.setattr(searcher, "_search_flamp_direct", _mock_flamp_direct)

    raw = _make_raw(name="БурПро", source="2gis", source_id="70000001012345")

    fake_review = RawReview(source="flamp", text="Хорошая компания")
    flamp_parser = next(p for p in rs_module._PARSERS if p.source_name == "flamp")
    with patch.object(flamp_parser, "safe_parse", AsyncMock(return_value=[fake_review])) as mock_parse:
        results = await searcher.search(raw)

    # Check that the constructed Flamp URL was dispatched
    mock_parse.assert_called()
    call_url = mock_parse.call_args[0][0]
    assert call_url == "https://omsk.flamp.ru/firm/70000001012345"
    assert results


async def test_search_flamp_direct_attempts_without_proxy_pool(searcher, monkeypatch):
    monkeypatch.setattr("src.enrichment.review_searcher.enrichment_proxy_manager._proxies", [])

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("src.enrichment.review_searcher.httpx.AsyncClient", DummyAsyncClient)

    async def _mock_flamp_api_search(client, query):
        return ["https://omsk.flamp.ru/firm/70000001012345"]

    async def _mock_flamp_html_search(client, query):
        return []

    monkeypatch.setattr(searcher, "_flamp_api_search", _mock_flamp_api_search)
    monkeypatch.setattr(searcher, "_flamp_html_search", _mock_flamp_html_search)

    result = await searcher._search_flamp_direct("ООО БурПро Омск")
    assert result == ["https://omsk.flamp.ru/firm/70000001012345"]


async def test_search_flamp_direct_uses_phone_candidate(searcher, monkeypatch):
    seen_queries: list[str] = []

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr("src.enrichment.review_searcher.httpx.AsyncClient", DummyAsyncClient)
    monkeypatch.setattr("src.enrichment.review_searcher.enrichment_proxy_manager._proxies", [])

    async def _mock_flamp_api_search(client, query):
        seen_queries.append(query)
        return []

    async def _mock_flamp_html_search(client, query):
        seen_queries.append(query)
        if query == "+79991234567":
            return ["https://omsk.flamp.ru/firm/70000001012345"]
        return []

    monkeypatch.setattr(searcher, "_flamp_api_search", _mock_flamp_api_search)
    monkeypatch.setattr(searcher, "_flamp_html_search", _mock_flamp_html_search)

    result = await searcher._search_flamp_direct("ООО БурПро", ["+79991234567"])
    assert result == ["https://omsk.flamp.ru/firm/70000001012345"]
    assert "+79991234567" in seen_queries


async def test_flamp_html_search_normalizes_scheme_relative_links(searcher, monkeypatch):
    html = """
    <html><body>
      <a href="//omsk.flamp.ru/firm/burpro-12345678">Firm</a>
    </body></html>
    """

    class DummyResponse:
        status_code = 200
        text = html

    class DummyClient:
        async def get(self, url):
            return DummyResponse()

    urls = await searcher._flamp_html_search(DummyClient(), "БурПро")
    assert urls == ["https://omsk.flamp.ru/firm/burpro-12345678"]


# ---------------------------------------------------------------------------
# _clean_company_name
# ---------------------------------------------------------------------------


def test_clean_company_name_strips_ooo():
    assert _clean_company_name("ООО БурПро") == "БурПро"


def test_clean_company_name_strips_ip():
    assert _clean_company_name("ИП Петров Сантехника") == "Петров Сантехника"


def test_clean_company_name_strips_dash_suffix():
    assert _clean_company_name("БурПро — Бурение скважин Омск") == "БурПро"


def test_clean_company_name_strips_city():
    assert _clean_company_name("Строймонтаж Омск") == "Строймонтаж"


def test_clean_company_name_combined():
    assert _clean_company_name("ООО БурПро — Бурение скважин Омск") == "БурПро"


def test_clean_company_name_strips_service_suffix_after_slash():
    assert _clean_company_name("БурПро / бурение скважин") == "БурПро"


def test_build_flamp_queries_adds_phone():
    queries = _build_flamp_queries("ООО БурПро / бурение скважин", ["+79991234567"])
    assert queries[0] == "БурПро"
    assert "+79991234567" in queries
