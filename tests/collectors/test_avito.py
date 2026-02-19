"""Avito collector tests using a local HTTP server (no real network calls).

These tests launch a real Chromium browser via Playwright.
Mark: @pytest.mark.playwright
"""

import asyncio

import pytest

import src.collectors.avito as avito_module
from src.collectors.avito import AvitoCollector
from tests.conftest import FIXTURES_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _listing_html(base_url: str) -> str:
    """Return the listing fixture HTML, replacing placeholder hrefs with
    absolute URLs so Playwright can navigate to the profile pages."""
    html = (FIXTURES_DIR / "avito_listing.html").read_text(encoding="utf-8")
    # The listing HTML uses relative paths (/item/...); replace avito.ru with
    # the local server so _parse_card_basic builds correct absolute URLs.
    return html


def _profile_html() -> str:
    return (FIXTURES_DIR / "avito_profile.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.playwright
async def test_collect_two_companies(httpserver, monkeypatch):
    """Collector should parse 2 cards from the listing fixture."""
    # --- serve fixtures ---
    httpserver.expect_request("/omsk/uslugi").respond_with_data(
        _listing_html(httpserver.url_for("")),
        content_type="text/html; charset=utf-8",
    )
    # Profile pages (for both items in the listing fixture)
    for path in ["/item/bur-skvazhin-omsk_12345", "/item/santehnik-omsk_67890"]:
        httpserver.expect_request(path).respond_with_data(
            _profile_html(), content_type="text/html; charset=utf-8"
        )

    base = httpserver.url_for("").rstrip("/")

    # --- monkeypatch module globals so collector hits local server ---
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", httpserver.url_for("/omsk/uslugi"))

    # --- make sleeps instant ---
    async def _no_sleep(_=0, **__):
        pass
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    # --- disable proxy so Playwright uses direct connection ---
    monkeypatch.setattr("src.collectors.avito.proxy_manager.playwright_proxy", lambda: None)

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    assert len(companies) == 2

    names = {c.name_raw for c in companies}
    assert "ООО БурПро — Бурение скважин Омск" in names
    assert "ИП Петров — Сантехника и водопровод" in names


@pytest.mark.playwright
async def test_collect_source_ids_extracted(httpserver, monkeypatch):
    """source_id should be extracted from the numeric suffix of the URL."""
    httpserver.expect_request("/omsk/uslugi").respond_with_data(
        _listing_html(httpserver.url_for("")),
        content_type="text/html; charset=utf-8",
    )
    for path in ["/item/bur-skvazhin-omsk_12345", "/item/santehnik-omsk_67890"]:
        httpserver.expect_request(path).respond_with_data(
            _profile_html(), content_type="text/html; charset=utf-8"
        )

    base = httpserver.url_for("").rstrip("/")
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", httpserver.url_for("/omsk/uslugi"))

    async def _no_sleep(_=0, **__):
        pass
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("src.collectors.avito.proxy_manager.playwright_proxy", lambda: None)

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    source_ids = {c.source_id for c in companies}
    assert "12345" in source_ids
    assert "67890" in source_ids


@pytest.mark.playwright
async def test_collect_ratings_parsed(httpserver, monkeypatch):
    """Ratings and review counts should be parsed from listing cards."""
    httpserver.expect_request("/omsk/uslugi").respond_with_data(
        _listing_html(httpserver.url_for("")),
        content_type="text/html; charset=utf-8",
    )
    for path in ["/item/bur-skvazhin-omsk_12345", "/item/santehnik-omsk_67890"]:
        httpserver.expect_request(path).respond_with_data(
            _profile_html(), content_type="text/html; charset=utf-8"
        )

    base = httpserver.url_for("").rstrip("/")
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", httpserver.url_for("/omsk/uslugi"))

    async def _no_sleep(_=0, **__):
        pass
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("src.collectors.avito.proxy_manager.playwright_proxy", lambda: None)

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    burpro = next(c for c in companies if "БурПро" in (c.name_raw or ""))
    assert burpro.average_rating == pytest.approx(4.9)
    assert burpro.reviews_count == 23


@pytest.mark.playwright
async def test_collect_profile_phone_and_email(httpserver, monkeypatch):
    """Profile enrichment should extract phone, email, address and reviews."""
    httpserver.expect_request("/omsk/uslugi").respond_with_data(
        _listing_html(httpserver.url_for("")),
        content_type="text/html; charset=utf-8",
    )
    for path in ["/item/bur-skvazhin-omsk_12345", "/item/santehnik-omsk_67890"]:
        httpserver.expect_request(path).respond_with_data(
            _profile_html(), content_type="text/html; charset=utf-8"
        )

    base = httpserver.url_for("").rstrip("/")
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", httpserver.url_for("/omsk/uslugi"))

    async def _no_sleep(_=0, **__):
        pass
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("src.collectors.avito.proxy_manager.playwright_proxy", lambda: None)

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    burpro = next(c for c in companies if "БурПро" in (c.name_raw or ""))
    assert burpro.phones, "Expected phone to be extracted from profile"
    assert any("+7" in p or "913" in p for p in burpro.phones)

    assert burpro.emails == ["burpro@example.com"]
    assert burpro.addresses == ["Омск, ул. Тарская, 12"]

    assert len(burpro.reviews) == 2
    review_texts = {r.text for r in burpro.reviews}
    assert any("рекомендую" in t for t in review_texts)


@pytest.mark.playwright
async def test_collect_empty_listing(httpserver, monkeypatch):
    """An empty listing page should return an empty list."""
    empty_html = """<!DOCTYPE html><html><body></body></html>"""
    httpserver.expect_request("/omsk/uslugi").respond_with_data(
        empty_html, content_type="text/html; charset=utf-8"
    )

    base = httpserver.url_for("").rstrip("/")
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", httpserver.url_for("/omsk/uslugi"))

    async def _no_sleep(_=0, **__):
        pass
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("src.collectors.avito.proxy_manager.playwright_proxy", lambda: None)

    collector = AvitoCollector()
    companies = await collector.collect("несуществующий запрос")
    assert companies == []


# ---------------------------------------------------------------------------
# Parse helpers — pure Python, no browser
# ---------------------------------------------------------------------------


def test_parse_float_normal():
    from src.collectors.avito import _parse_float
    assert _parse_float("4.9") == pytest.approx(4.9)
    assert _parse_float("4,2") == pytest.approx(4.2)
    assert _parse_float("") is None
    assert _parse_float("нет оценки") is None


def test_parse_int_normal():
    from src.collectors.avito import _parse_int
    assert _parse_int("23 отзыва") == 23
    assert _parse_int("8 отзывов") == 8
    assert _parse_int("") is None
