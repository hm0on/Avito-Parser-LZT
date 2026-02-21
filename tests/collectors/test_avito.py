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

    # --- disable proxy and cookies so collector hits local server directly ---
    monkeypatch.setattr("src.collectors.avito.proxy_manager.playwright_proxy", lambda: None)
    monkeypatch.setattr("src.collectors.avito.proxy_manager.get_next", lambda: None)
    monkeypatch.setattr(avito_module, "_build_cookies_provider", lambda: None)

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    assert len(companies) >= 2

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
    monkeypatch.setattr("src.collectors.avito.proxy_manager.get_next", lambda: None)

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
    monkeypatch.setattr("src.collectors.avito.proxy_manager.get_next", lambda: None)

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    burpro = next(c for c in companies if "БурПро" in (c.name_raw or ""))
    assert burpro.average_rating == pytest.approx(4.9)
    assert burpro.reviews_count == 23


@pytest.mark.playwright
async def test_enrich_reviews_from_profile(httpserver, monkeypatch):
    """enrich_reviews should extract reviews via ad page → profile → API."""
    from src.collectors.base import RawCompany
    import json as _json

    # Serve ad page with a seller link
    ad_html = '<html><body><a data-marker="seller-link" href="/user/abc123/profile">Seller</a></body></html>'
    httpserver.expect_request("/item/test_12345").respond_with_data(
        ad_html, content_type="text/html; charset=utf-8",
    )
    # Serve profile page with embedded ratings API path
    profile_html = (
        '<html><body>'
        '<script>&quot;nextPage&quot;:&quot;/web/6/user/abc123/ratings&quot;</script>'
        '</body></html>'
    )
    httpserver.expect_request("/user/abc123/profile").respond_with_data(
        profile_html, content_type="text/html; charset=utf-8",
    )
    # Serve ratings API with reviews (Avito API v6 entries format)
    ratings_json = _json.dumps({
        "entries": [
            {"type": "score", "value": {"scoreFloat": 4.9}},
            {"type": "rating", "value": {
                "title": "Иван", "score": 5,
                "textSections": [{"text": "Отличная работа, рекомендую! Качественно и в срок."}],
            }},
            {"type": "rating", "value": {
                "title": "Мария", "score": 4,
                "textSections": [{"text": "Работа выполнена хорошо, цена соответствует рынку."}],
            }},
        ]
    })
    httpserver.expect_request("/web/6/user/abc123/ratings").respond_with_data(
        ratings_json, content_type="application/json",
    )

    base = httpserver.url_for("").rstrip("/")
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)

    async def _no_sleep(_=0, **__):
        pass
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("src.collectors.avito.proxy_manager.get_next", lambda: None)
    monkeypatch.setattr(avito_module, "_build_cookies_provider", lambda: None)

    company = RawCompany(
        source="avito",
        name_raw="ООО БурПро",
        source_id="12345",
        source_link=f"{base}/item/test_12345",
        reviews_count=2,
    )

    collector = AvitoCollector()
    await collector.enrich_reviews([company])

    assert len(company.reviews) == 2
    assert company.reviews[0].rating == 5.0
    assert company.reviews[0].author == "Иван"
    assert "рекомендую" in company.reviews[0].text


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
    monkeypatch.setattr("src.collectors.avito.proxy_manager.get_next", lambda: None)

    collector = AvitoCollector()
    companies = await collector.collect("несуществующий запрос")
    assert companies == []


# ---------------------------------------------------------------------------
# Parse helpers — pure Python, no browser
# ---------------------------------------------------------------------------


def test_parse_float_normal():
    from src.collectors.base import parse_float
    assert parse_float("4.9") == pytest.approx(4.9)
    assert parse_float("4,2") == pytest.approx(4.2)
    assert parse_float("") is None
    assert parse_float("нет оценки") is None


def test_parse_int_normal():
    from src.collectors.base import parse_int
    assert parse_int("23 отзыва") == 23
    assert parse_int("8 отзывов") == 8
    assert parse_int("") is None


# ---------------------------------------------------------------------------
# Seller review extraction helpers — pure Python, no browser
# ---------------------------------------------------------------------------


def test_extract_seller_path_data_marker():
    from src.collectors.avito import _extract_seller_path

    html = '<html><body><a data-marker="seller-link" href="/user/abc123/profile">Seller</a></body></html>'
    assert _extract_seller_path(html) == "/user/abc123/profile"


def test_extract_seller_path_generic_user_link():
    from src.collectors.avito import _extract_seller_path

    html = '<html><body><a href="/user/seller42">View seller</a></body></html>'
    assert _extract_seller_path(html) == "/user/seller42/profile"


def test_extract_seller_path_skips_login():
    from src.collectors.avito import _extract_seller_path

    html = '<html><body><a href="/user/login">Login</a></body></html>'
    assert _extract_seller_path(html) is None


def test_extract_seller_path_regex_fallback():
    from src.collectors.avito import _extract_seller_path

    html = '<html><body><script>var url = "/user/abcdef12345/items";</script></body></html>'
    assert _extract_seller_path(html) == "/user/abcdef12345/profile"


def test_parse_reviews_from_html_data_marker():
    from src.collectors.avito import _parse_reviews_from_html

    html = """
    <html><body>
    <div data-marker="review-item">
        <span>Отличная работа, рекомендую всем! Пробурили скважину быстро.</span>
    </div>
    <div data-marker="review-item">
        <span>Работа выполнена в срок, качество хорошее. Спасибо большое.</span>
    </div>
    </body></html>
    """
    reviews = _parse_reviews_from_html(html, "https://avito.ru/item/123")
    assert len(reviews) == 2
    assert reviews[0].source == "avito"
    assert "рекомендую" in reviews[0].text


def test_parse_reviews_from_html_class_based():
    from src.collectors.avito import _parse_reviews_from_html

    html = """
    <html><body>
    <div class="review-card">Хорошая компания, работают быстро и качественно.</div>
    </body></html>
    """
    reviews = _parse_reviews_from_html(html, "https://avito.ru/item/123")
    assert len(reviews) == 1


def test_parse_ratings_json_entries_format():
    """Avito API v6 returns entries with type=rating."""
    from src.collectors.avito import _parse_ratings_json

    data = {
        "entries": [
            {"type": "score", "value": {"scoreFloat": 4.9, "reviewCount": 20}},
            {
                "type": "rating",
                "value": {
                    "title": "Карина",
                    "titleCaption": "Покупатель",
                    "score": 3,
                    "textSections": [
                        {"text": "Продавец странный. Заказывала кеды, кеды пришли маленькие."}
                    ],
                },
            },
            {
                "type": "rating",
                "value": {
                    "title": "Марина",
                    "score": 5,
                    "textSections": [
                        {"text": "Товар пришел быстро, качество отличное, по размеру подошел."}
                    ],
                },
            },
            {
                "type": "rating",
                "value": {
                    "title": "Анон",
                    "score": 5,
                    "textSections": [{"text": "ok"}],  # too short, should be skipped
                },
            },
        ]
    }
    reviews = _parse_ratings_json(data, "https://avito.ru/user/abc/profile")
    assert len(reviews) == 2
    assert reviews[0].author == "Карина"
    assert reviews[0].rating == 3.0
    assert "кеды" in reviews[0].text
    assert reviews[1].author == "Марина"
    assert reviews[1].rating == 5.0


def test_walk_json_for_reviews():
    from src.collectors.avito import _walk_json_for_reviews

    data = {
        "result": {
            "reviews": [
                {"text": "Отличная работа, всё сделали в срок!", "score": 5, "sender": {"name": "Иван"}},
                {"text": "Качество хорошее, но цена высокая.", "score": 4, "sender": {"name": "Мария"}},
                {"text": "ok", "score": 3},  # too short, should be skipped
            ]
        }
    }
    reviews = _walk_json_for_reviews(data, "https://avito.ru/item/123")
    assert len(reviews) == 2
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "Иван"
    assert reviews[1].rating == 4.0
