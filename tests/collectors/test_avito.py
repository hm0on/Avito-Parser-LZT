"""Unit tests for Avito collector logic with mocked browser client."""

import asyncio
import html
import json

import pytest

import src.collectors.avito as avito_module
from src.collectors.avito import AvitoCollector, _ProxyStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_fake_client(monkeypatch, *, html_by_url=None, json_by_url=None):
    html_by_url = html_by_url or {}
    json_by_url = json_by_url or {}
    calls: list[tuple[str, str, str | None]] = []

    class FakeBrowserClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get_html(self, url: str, *, referer: str | None = None):
            calls.append(("html", url, referer))
            payload = html_by_url.get(url)
            if payload is None:
                return 404, ""
            return payload

        async def get_profile_reviews_html(
            self,
            profile_url: str,
            *,
            referer: str | None = None,
            max_reviews: int,
        ):
            calls.append(("profile_html", profile_url, referer))
            payload = html_by_url.get(profile_url)
            if payload is None:
                return "", 0
            return payload[1], 0

        async def get_json(self, url: str, *, referer: str | None = None):
            calls.append(("json", url, referer))
            payload = json_by_url.get(url)
            if payload is None:
                return 404, None, "", "application/json"
            return payload

    monkeypatch.setattr(avito_module, "_PlaywrightBrowserClient", FakeBrowserClient)
    monkeypatch.setattr(
        avito_module.avito_proxy_manager,
        "_proxies",
        ["http://user:pass@127.0.0.1:8080"],
    )
    monkeypatch.setattr(
        "src.collectors.avito.avito_proxy_manager.playwright_proxy",
        lambda *args, **kwargs: {
            "server": "http://127.0.0.1:8080",
            "username": "user",
            "password": "pass",
        },
    )
    return calls


# ---------------------------------------------------------------------------
# Collector tests
# ---------------------------------------------------------------------------


async def test_collect_two_companies(avito_listing_html, no_sleep, monkeypatch):
    base = "https://example.test"
    search = f"{base}/omsk/uslugi"

    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", search)

    page1 = f"{search}?q=%D0%B1%D1%83%D1%80%D0%B5%D0%BD%D0%B8%D0%B5+%D1%81%D0%BA%D0%B2%D0%B0%D0%B6%D0%B8%D0%BD"
    page2 = page1 + "&p=2"

    _install_fake_client(
        monkeypatch,
        html_by_url={
            page1: (200, avito_listing_html),
            page2: (200, "<html><body></body></html>"),
        },
    )

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    assert len(companies) == 2
    names = {c.name_raw for c in companies}
    assert "ООО БурПро — Бурение скважин Омск" in names
    assert "ИП Петров — Сантехника и водопровод" in names


async def test_collect_source_ids_extracted(avito_listing_html, no_sleep, monkeypatch):
    base = "https://example.test"
    search = f"{base}/omsk/uslugi"

    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", search)

    page1 = f"{search}?q=%D0%B1%D1%83%D1%80%D0%B5%D0%BD%D0%B8%D0%B5+%D1%81%D0%BA%D0%B2%D0%B0%D0%B6%D0%B8%D0%BD"

    _install_fake_client(monkeypatch, html_by_url={page1: (200, avito_listing_html)})

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    source_ids = {c.source_id for c in companies}
    assert "12345" in source_ids
    assert "67890" in source_ids


async def test_collect_ratings_parsed(avito_listing_html, no_sleep, monkeypatch):
    base = "https://example.test"
    search = f"{base}/omsk/uslugi"

    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", search)

    page1 = f"{search}?q=%D0%B1%D1%83%D1%80%D0%B5%D0%BD%D0%B8%D0%B5+%D1%81%D0%BA%D0%B2%D0%B0%D0%B6%D0%B8%D0%BD"

    _install_fake_client(monkeypatch, html_by_url={page1: (200, avito_listing_html)})

    collector = AvitoCollector()
    companies = await collector.collect("бурение скважин")

    burpro = next(c for c in companies if "БурПро" in (c.name_raw or ""))
    assert burpro.average_rating == pytest.approx(4.9)
    assert burpro.reviews_count == 23


def test_parse_html_saves_seller_link_and_prefers_brand(monkeypatch):
    base = "https://example.test"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)

    html_text = """
    <html><body>
      <div data-item-id="42">
        <a data-marker="item-title" href="/item/test_42">Company 42</a>
        <a href="/user/seller42/profile">User Profile</a>
        <a href="/brands/ba2fa4036a89b34718ec3745c5c55b98?src=search_seller_info&amp;iid=42">Brand Page</a>
        <span data-marker="seller-info/summary">3 reviews</span>
      </div>
    </body></html>
    """

    companies = avito_module._parse_html(html_text, "test")
    assert len(companies) == 1
    payload = companies[0].raw_payload or {}
    assert payload.get("seller_link") == (
        f"{base}/brands/ba2fa4036a89b34718ec3745c5c55b98?src=search_seller_info&iid=42"
    )


async def test_enrich_reviews_from_profile(no_sleep, monkeypatch):
    from src.collectors.base import RawCompany

    base = "https://example.test"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)

    ad_url = f"{base}/item/test_12345"
    profile_url = f"{base}/user/abc123/profile"
    ratings_url = f"{base}/web/6/user/abc123/ratings"

    ad_html = '<html><body><a data-marker="seller-link" href="/user/abc123/profile">Seller</a></body></html>'
    profile_html = (
        '<html><body>'
        '<script>&quot;nextPage&quot;:&quot;/web/6/user/abc123/ratings&quot;</script>'
        '</body></html>'
    )
    ratings_data = {
        "entries": [
            {"type": "score", "value": {"scoreFloat": 4.9}},
            {
                "type": "rating",
                "value": {
                    "title": "Иван",
                    "score": 5,
                    "textSections": [{"text": "Отличная работа, рекомендую! Качественно и в срок."}],
                },
            },
            {
                "type": "rating",
                "value": {
                    "title": "Мария",
                    "score": 4,
                    "textSections": [{"text": "Работа выполнена хорошо, цена соответствует рынку."}],
                },
            },
        ]
    }

    _install_fake_client(
        monkeypatch,
        html_by_url={
            ad_url: (200, ad_html),
            profile_url: (200, profile_html),
        },
        json_by_url={
            ratings_url: (200, ratings_data, "{}", "application/json"),
        },
    )

    company = RawCompany(
        source="avito",
        name_raw="ООО БурПро",
        source_id="12345",
        source_link=ad_url,
        reviews_count=2,
    )

    collector = AvitoCollector()
    await collector.enrich_reviews([company])

    assert len(company.reviews) == 2
    assert company.reviews[0].rating == 5.0
    assert company.reviews[0].author == "Иван"
    assert "рекомендую" in company.reviews[0].text


async def test_enrich_reviews_from_ad_page_review_block(no_sleep, monkeypatch):
    from src.collectors.base import RawCompany

    base = "https://example.test"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)

    ad_url = f"{base}/item/test_555"
    ad_html = (
        "<html><body>"
        '<div data-marker="review(0)">'
        '<h5 data-marker="review(0)/header/title">Ivan</h5>'
        '<meta itemprop="ratingValue" content="5" />'
        '<p data-marker="review(0)/text-section/text">Great work done quickly and carefully.</p>'
        "</div>"
        "</body></html>"
    )

    calls = _install_fake_client(
        monkeypatch,
        html_by_url={
            ad_url: (200, ad_html),
        },
    )

    company = RawCompany(
        source="avito",
        name_raw="Ad Reviews Company",
        source_id="555",
        source_link=ad_url,
        reviews_count=1,
    )

    collector = AvitoCollector()
    await collector.enrich_reviews([company])

    assert len(company.reviews) == 1
    assert company.reviews[0].author == "Ivan"
    assert company.reviews[0].rating == 5.0
    profile_calls = [c for c in calls if c[0] == "profile_html"]
    assert profile_calls == []


async def test_enrich_reviews_from_rating_caption_link(no_sleep, monkeypatch):
    from src.collectors.base import RawCompany

    base = "https://example.test"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)

    ad_url = f"{base}/omsk/predlozheniya_uslug/burenie_skvazhin_7973475046"
    profile_url = f"{base}/user/902d2bbf0265c4d1a0bcceb4b4a1b5bc/profile?id=7973475046&src=item"
    ad_html = (
        "<html><body>"
        '<a data-marker="rating-caption/rating" '
        f'href="{profile_url}">5 отзывов</a>'
        "</body></html>"
    )
    profile_html = (
        "<html><body>"
        '<div data-marker="review(0)">'
        '<h5 data-marker="review(0)/header/title">Евгений</h5>'
        '<meta itemprop="ratingValue" content="5" />'
        '<p data-marker="review(0)/text-section/text">Всё норм. Работа произведена в полном объёме.</p>'
        "</div>"
        "</body></html>"
    )

    calls = _install_fake_client(
        monkeypatch,
        html_by_url={
            ad_url: (200, ad_html),
            profile_url: (200, profile_html),
        },
    )

    company = RawCompany(
        source="avito",
        name_raw="Юрий мини-экскаватор",
        source_id="7973475046",
        source_link=ad_url,
        reviews_count=5,
    )

    collector = AvitoCollector()
    await collector.enrich_reviews([company])

    assert len(company.reviews) == 1
    assert company.reviews[0].author == "Евгений"
    assert company.reviews[0].rating == 5.0
    profile_calls = [c for c in calls if c[0] == "profile_html"]
    assert profile_calls == [("profile_html", profile_url, ad_url)]


async def test_enrich_reviews_from_direct_profile_link(no_sleep, monkeypatch):
    from src.collectors.base import RawCompany

    base = "https://example.test"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)

    profile_url = f"{base}/user/abc123/profile"
    ratings_url = f"{base}/web/6/user/abc123/ratings"
    profile_html = (
        '<html><body>'
        '<script>&quot;nextPage&quot;:&quot;/web/6/user/abc123/ratings&quot;</script>'
        "</body></html>"
    )
    ratings_data = {
        "entries": [
            {
                "type": "rating",
                "value": {
                    "title": "User 1",
                    "score": 5,
                    "textSections": [{"text": "Very good work, all done quickly and carefully."}],
                },
            },
            {
                "type": "rating",
                "value": {
                    "title": "User 2",
                    "score": 4,
                    "textSections": [{"text": "Normal result, agreements fulfilled, no complaints."}],
                },
            },
        ]
    }

    _install_fake_client(
        monkeypatch,
        html_by_url={
            profile_url: (200, profile_html),
        },
        json_by_url={
            ratings_url: (200, ratings_data, "{}", "application/json"),
        },
    )

    company = RawCompany(
        source="avito",
        name_raw="Profile Company",
        source_id="u123",
        source_link=profile_url,
        reviews_count=2,
    )

    collector = AvitoCollector()
    await collector.enrich_reviews([company])

    assert len(company.reviews) == 2
    assert company.reviews[0].author == "User 1"
    assert company.reviews[0].rating == 5.0


async def test_enrich_reviews_uses_seller_link_from_raw_payload(no_sleep, monkeypatch):
    from src.collectors.base import RawCompany

    base = "https://example.test"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)

    ad_url = f"{base}/item/test_111"
    brand_url = f"{base}/brands/ba2fa4036a89b34718ec3745c5c55b98"
    profile_html = (
        "<html><body>"
        '<div data-marker="review(0)">'
        '<h5 data-marker="review(0)/header/title">Brand Buyer</h5>'
        '<meta itemprop="ratingValue" content="5" />'
        '<p data-marker="review(0)/text-section/text">Great job, fast and clean work done.</p>'
        "</div>"
        "</body></html>"
    )

    calls = _install_fake_client(
        monkeypatch,
        html_by_url={
            brand_url: (200, profile_html),
        },
    )

    company = RawCompany(
        source="avito",
        name_raw="Brand Company",
        source_id="111",
        source_link=ad_url,
        reviews_count=1,
        raw_payload={"seller_link": brand_url},
    )

    collector = AvitoCollector()
    await collector.enrich_reviews([company])

    assert len(company.reviews) == 1
    profile_calls = [call for call in calls if call[0] == "profile_html"]
    assert profile_calls == [("profile_html", brand_url, ad_url)]
    html_calls = [call for call in calls if call[0] == "html"]
    assert html_calls == []


async def test_collect_empty_listing(no_sleep, monkeypatch):
    base = "https://example.test"
    search = f"{base}/omsk/uslugi"

    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", search)

    page1 = f"{search}?q=%D0%BD%D0%B5%D1%81%D1%83%D1%89%D0%B5%D1%81%D1%82%D0%B2%D1%83%D1%8E%D1%89%D0%B8%D0%B9+%D0%B7%D0%B0%D0%BF%D1%80%D0%BE%D1%81"

    _install_fake_client(monkeypatch, html_by_url={page1: (200, "<html><body></body></html>")})

    collector = AvitoCollector()
    companies = await collector.collect("несуществующий запрос")
    assert companies == []


async def test_enrich_reviews_second_pass_only_for_cards_with_reviews(monkeypatch):
    from src.collectors.base import RawCompany, RawReview

    calls: list[str] = []

    async def _fake_fetch(self, company):
        calls.append(company.source_id or "")
        return [RawReview(source="avito", text="ok review text", rating=5.0, author="A")]

    monkeypatch.setattr(AvitoCollector, "_fetch_seller_reviews_with_retries", _fake_fetch, raising=False)

    collector = AvitoCollector()
    collector._review_workers = 1
    companies = [
        RawCompany(source="avito", source_id="1", source_link="https://x/item/1", reviews_count=3),
        RawCompany(source="avito", source_id="2", source_link="https://x/item/2", reviews_count=0),
        RawCompany(source="avito", source_id="4", source_link="https://x/item/4", reviews_count=None),
        RawCompany(source="yandex", source_id="3", source_link="https://x/item/3", reviews_count=8),
    ]
    await collector.enrich_reviews(companies)

    assert calls == ["1"]
    assert len(companies[0].reviews) == 1
    assert companies[1].reviews == []
    assert companies[2].reviews == []


async def test_collect_stops_on_max_pages_20(no_sleep, monkeypatch):
    base = "https://example.test"
    search = f"{base}/omsk/uslugi"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", search)

    def _page_html(page: int, next_href: str | None) -> str:
        next_block = (
            f'<a data-marker="pagination-button/nextPage" href="{next_href}">next</a>'
            if next_href
            else ""
        )
        return (
            "<html><body>"
            f'<div data-item-id="{page}">'
            f'<a data-marker="item-title" href="/item/{page}">Company {page}</a>'
            "</div>"
            f"{next_block}"
            "</body></html>"
        )

    page1 = f"{search}?q=test"
    html_by_url = {
        page1: (200, _page_html(1, "/omsk/uslugi?q=test&p=2")),
    }
    for p in range(2, 26):
        url = f"{base}/omsk/uslugi?q=test&p={p}"
        next_href = f"/omsk/uslugi?q=test&p={p+1}" if p < 25 else None
        html_by_url[url] = (200, _page_html(p, next_href))

    calls = _install_fake_client(monkeypatch, html_by_url=html_by_url)

    collector = AvitoCollector()
    collector._max_pages = 20
    companies = await collector.collect("test")

    html_calls = [c for c in calls if c[0] == "html"]
    assert len(html_calls) == 20
    assert len(companies) == 20


async def test_collect_stops_on_page_title_city_count(no_sleep, monkeypatch):
    base = "https://example.test"
    search = f"{base}/omsk/uslugi"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", search)

    def _cards(start: int, count: int) -> str:
        chunks = []
        for i in range(start, start + count):
            chunks.append(
                f'<div data-item-id="{i}"><a data-marker="item-title" href="/item/{i}">Company {i}</a></div>'
            )
        return "".join(chunks)

    page1 = f"{search}?q=test"
    page2 = f"{base}/omsk/uslugi?q=test&p=2"
    page3 = f"{base}/omsk/uslugi?q=test&p=3"

    html_by_url = {
        page1: (
            200,
            (
                "<html><body>"
                '<span data-marker="page-title/count">63</span>'
                f"{_cards(1, 50)}"
                '<a data-marker="pagination-button/nextPage" href="/omsk/uslugi?q=test&p=2">next</a>'
                "</body></html>"
            ),
        ),
        page2: (
            200,
            (
                "<html><body>"
                f"{_cards(51, 50)}"
                '<a data-marker="pagination-button/nextPage" href="/omsk/uslugi?q=test&p=3">next</a>'
                "</body></html>"
            ),
        ),
        page3: (
            200,
            (
                "<html><body>"
                f"{_cards(101, 50)}"
                "</body></html>"
            ),
        ),
    }

    calls = _install_fake_client(monkeypatch, html_by_url=html_by_url)
    collector = AvitoCollector()
    collector._max_pages = 20
    companies = await collector.collect("test")

    html_calls = [c for c in calls if c[0] == "html"]
    assert len(html_calls) == 2
    assert len(companies) == 63


async def test_collect_stops_on_current_city_boundary_from_mfe_state(no_sleep, monkeypatch):
    base = "https://example.test"
    search = f"{base}/omsk/uslugi"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", search)

    def _cards(start: int, count: int) -> str:
        chunks = []
        for i in range(start, start + count):
            chunks.append(
                f'<div data-item-id="{i}"><a data-marker="item-title" href="/item/{i}">Company {i}</a></div>'
            )
        return "".join(chunks)

    def _mfe_state(local_ids: set[int], all_ids: list[int]) -> str:
        items = []
        for item_id in all_ids:
            items.append(
                {
                    "id": item_id,
                    "location": {"isCurrent": item_id in local_ids},
                }
            )
        payload = {"state": {"data": {"catalog": {"items": items}}}}
        escaped = html.escape(json.dumps(payload, ensure_ascii=False))
        return f'<script type="mime/invalid" data-mfe-state="true">{escaped}</script>'

    page1 = f"{search}?q=test"
    page2 = f"{base}/omsk/uslugi?q=test&p=2"
    page3 = f"{base}/omsk/uslugi?q=test&p=3"

    local_page1 = set(range(1, 51))
    local_page2 = set(range(51, 64))
    ids_page1 = list(range(1, 51))
    ids_page2 = list(range(51, 101))
    ids_page3 = list(range(101, 151))

    html_by_url = {
        page1: (
            200,
            (
                "<html><body>"
                '<span data-marker="page-title/count">39704</span>'
                f"{_cards(1, 50)}"
                f"{_mfe_state(local_page1, ids_page1)}"
                '<a data-marker="pagination-button/nextPage" href="/omsk/uslugi?q=test&p=2">next</a>'
                "</body></html>"
            ),
        ),
        page2: (
            200,
            (
                "<html><body>"
                f"{_cards(51, 50)}"
                f"{_mfe_state(local_page2, ids_page2)}"
                '<a data-marker="pagination-button/nextPage" href="/omsk/uslugi?q=test&p=3">next</a>'
                "</body></html>"
            ),
        ),
        page3: (
            200,
            (
                "<html><body>"
                f"{_cards(101, 50)}"
                f"{_mfe_state(set(), ids_page3)}"
                "</body></html>"
            ),
        ),
    }

    calls = _install_fake_client(monkeypatch, html_by_url=html_by_url)
    collector = AvitoCollector()
    collector._max_pages = 20
    companies = await collector.collect("test")

    html_calls = [c for c in calls if c[0] == "html"]
    assert len(html_calls) == 3
    assert len(companies) == 150


async def test_collect_uses_fallback_next_page_when_next_link_missing(no_sleep, monkeypatch):
    base = "https://example.test"
    search = f"{base}/omsk/uslugi"
    monkeypatch.setattr(avito_module, "AVITO_BASE", base)
    monkeypatch.setattr(avito_module, "AVITO_SEARCH", search)

    def _cards(start: int, count: int) -> str:
        chunks = []
        for i in range(start, start + count):
            chunks.append(
                f'<div data-item-id="{i}"><a data-marker="item-title" href="/item/{i}">Company {i}</a></div>'
            )
        return "".join(chunks)

    page1 = f"{search}?q=test"
    page2 = f"{search}?q=test&p=2"

    html_by_url = {
        page1: (
            200,
            (
                "<html><body>"
                '<span data-marker="page-title/count">58</span>'
                f"{_cards(1, 50)}"
                "</body></html>"
            ),
        ),
        page2: (
            200,
            (
                "<html><body>"
                f"{_cards(51, 8)}"
                "</body></html>"
            ),
        ),
    }

    calls = _install_fake_client(monkeypatch, html_by_url=html_by_url)
    collector = AvitoCollector()
    collector._max_pages = 20
    companies = await collector.collect("test")

    html_calls = [c for c in calls if c[0] == "html"]
    assert len(html_calls) == 2
    assert companies and len(companies) == 58


# ---------------------------------------------------------------------------
# Parse helpers
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
# Seller review extraction helpers
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


def test_extract_seller_path_skips_review_slug():
    from src.collectors.avito import _extract_seller_path

    html = '<html><body><a href="/user/review">Review</a></body></html>'
    assert _extract_seller_path(html) is None


def test_extract_seller_path_regex_fallback():
    from src.collectors.avito import _extract_seller_path

    html = '<html><body><script>var url = "/user/abcdef12345/items";</script></body></html>'
    assert _extract_seller_path(html) == "/user/abcdef12345/profile"


def test_extract_seller_path_supports_brands_url():
    from src.collectors.avito import _extract_seller_path

    html = '<html><body><a href="/brands/ba2fa4036a89b34718ec3745c5c55b98?src=search_seller_info">Brand</a></body></html>'
    assert (
        _extract_seller_path(html)
        == "/brands/ba2fa4036a89b34718ec3745c5c55b98?src=search_seller_info"
    )


def test_extract_seller_path_prefers_brands_over_user():
    from src.collectors.avito import _extract_seller_path

    html = (
        "<html><body>"
        '<a data-marker="seller-link" href="/user/abc123/profile">User</a>'
        '<a href="/brands/ba2fa4036a89b34718ec3745c5c55b98?src=search_seller_info">Brand</a>'
        "</body></html>"
    )
    assert (
        _extract_seller_path(html)
        == "/brands/ba2fa4036a89b34718ec3745c5c55b98?src=search_seller_info"
    )


def test_extract_rating_caption_path():
    from src.collectors.avito import _extract_rating_caption_path

    html = (
        '<html><body>'
        '<a data-marker="rating-caption/rating" '
        'href="https://www.avito.ru/user/902d2bbf0265c4d1a0bcceb4b4a1b5bc/profile?id=7973475046&src=item">'
        "5 отзывов"
        "</a>"
        "</body></html>"
    )
    assert (
        _extract_rating_caption_path(html)
        == "/user/902d2bbf0265c4d1a0bcceb4b4a1b5bc/profile?id=7973475046&src=item"
    )


def test_extract_seller_path_keeps_profile_id_query():
    from src.collectors.avito import _extract_seller_path

    html = '<html><body><a data-marker="seller-link" href="/user/dca3546e3c3b6220ef0d35ed82931c0c/profile?id=7872344179">Seller</a></body></html>'
    assert _extract_seller_path(html) == "/user/dca3546e3c3b6220ef0d35ed82931c0c/profile?id=7872344179"


def test_extract_profile_url_from_source_link_keeps_iid_query():
    from src.collectors.avito import _extract_profile_url_from_source_link

    source_link = "https://www.avito.ru/brands/391951ef61c9fe729a87110f0ef220b8?src=search_seller_info&iid=3824911571"
    assert (
        _extract_profile_url_from_source_link(source_link)
        == "https://www.avito.ru/brands/391951ef61c9fe729a87110f0ef220b8?src=search_seller_info&iid=3824911571"
    )


def test_extract_profile_url_from_source_link_skips_review_slug():
    from src.collectors.avito import _extract_profile_url_from_source_link

    assert _extract_profile_url_from_source_link("https://www.avito.ru/user/review") is None


def test_extract_next_page_url_from_pagination_nav():
    from src.collectors.avito import _extract_next_page_url

    html = (
        '<html><body><nav>'
        '<a data-marker="pagination-button/nextPage" href="/ekaterinburg/vakansii?p=6&context=abc">Next</a>'
        "</nav></body></html>"
    )
    assert _extract_next_page_url(html) == "/ekaterinburg/vakansii?p=6&context=abc"


def test_increment_page_url_fallback():
    from src.collectors.avito import _increment_page_url

    assert _increment_page_url("https://www.avito.ru/omsk/predlozheniya_uslug?q=test") == (
        "https://www.avito.ru/omsk/predlozheniya_uslug?q=test&p=2"
    )
    assert _increment_page_url("https://www.avito.ru/omsk/predlozheniya_uslug?q=test&p=2") == (
        "https://www.avito.ru/omsk/predlozheniya_uslug?q=test&p=3"
    )


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


def test_parse_reviews_from_html_brand_markup():
    from src.collectors.avito import _parse_reviews_from_html

    html = """
    <html><body>
      <div data-marker="review(0)">
        <h5 data-marker="review(0)/header/title">Андрей</h5>
        <div data-marker="review(0)/score">
          <meta itemprop="ratingValue" content="5" />
        </div>
        <p data-marker="review(0)/text-section/text">Все супер рекомендую</p>
      </div>
    </body></html>
    """
    reviews = _parse_reviews_from_html(html, "https://www.avito.ru/item/1")
    assert len(reviews) == 1
    assert reviews[0].author == "Андрей"
    assert reviews[0].rating == pytest.approx(5.0)
    assert reviews[0].text == "Все супер рекомендую"


def test_reviews_are_capped_by_max_reviews_per_company():
    from src.collectors.avito import _parse_reviews_from_html

    html = """
    <html><body>
      <div data-marker="review(0)"><h5 data-marker="review(0)/header/title">A</h5><meta itemprop="ratingValue" content="5"/><p data-marker="review(0)/text-section/text">Текст отзыва один длинный</p></div>
      <div data-marker="review(1)"><h5 data-marker="review(1)/header/title">B</h5><meta itemprop="ratingValue" content="4"/><p data-marker="review(1)/text-section/text">Текст отзыва два длинный</p></div>
      <div data-marker="review(2)"><h5 data-marker="review(2)/header/title">C</h5><meta itemprop="ratingValue" content="3"/><p data-marker="review(2)/text-section/text">Текст отзыва три длинный</p></div>
    </body></html>
    """
    reviews = _parse_reviews_from_html(html, "https://www.avito.ru/item/2", max_reviews=2)
    assert len(reviews) == 2


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
                    "textSections": [{"text": "ok"}],
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
                {"text": "ok", "score": 3},
            ]
        }
    }
    reviews = _walk_json_for_reviews(data, "https://avito.ru/item/123")
    assert len(reviews) == 2
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "Иван"
    assert reviews[1].rating == 4.0


def test_is_proxy_error_detects_tunnel_issue():
    from src.collectors.avito import _is_proxy_error

    err = RuntimeError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED")
    assert _is_proxy_error(err) is True


def test_socks_fallback_switches_scheme():
    collector = AvitoCollector()
    collector._proxy_scheme = "socks5"
    collector._proxy_fallback_scheme = "http"
    err = RuntimeError("BrowserType.launch: Browser does not support socks5 proxy authentication")

    collector._maybe_fallback_proxy_scheme(err)

    assert collector._proxy_scheme == "http"


def test_proxy_from_url_prefers_explicit_scheme_over_configured():
    collector = AvitoCollector()
    collector._proxy_scheme = "socks5"

    proxy = collector._proxy_from_url("http://user:pass@1.2.3.4:8080", scheme=collector._proxy_scheme)

    assert proxy is not None
    assert proxy["server"] == "http://1.2.3.4:8080"
    assert proxy["username"] == "user"
    assert proxy["password"] == "pass"


def test_retry_wait_is_capped_and_not_zero_for_proxy_errors():
    collector = AvitoCollector()
    collector._avito_proxy_urls = ["http://1", "http://2"]
    collector._request_min_interval_s = 6.0
    collector._retry_jitter_max_s = 0.0  # disable jitter for deterministic test

    assert collector._retry_wait_s(attempt=999, proxy_error=False) == 60.0
    assert collector._retry_wait_s(attempt=999, proxy_error=True) == 6.0


async def test_socks_bridge_close_cancels_client_tasks():
    from src.collectors.avito import _Socks5AuthBridge

    bridge = _Socks5AuthBridge(host="127.0.0.1", port=1080, username="u", password="p")
    sleeper = asyncio.create_task(asyncio.sleep(30))
    bridge._client_tasks.add(sleeper)  # type: ignore[attr-defined]

    await bridge.close()

    assert sleeper.cancelled() or sleeper.done()


async def test_get_html_recovers_partial_html_after_goto_error():
    class FakePage:
        async def goto(self, *_args, **_kwargs):
            raise RuntimeError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED")

        async def wait_for_load_state(self, *_args, **_kwargs):
            return None

        async def content(self):
            return (
                '<html><body>'
                '<div data-item-id="1"><a data-marker="item-title" href="/item/1">X</a></div>'
                "</body></html>"
            )

    client = avito_module._PlaywrightBrowserClient(
        proxy={"server": "socks5://u:p@127.0.0.1:1080"},
        headless=True,
        timeout_ms=180_000,
        browser_name="chromium",
    )
    client._page = FakePage()  # type: ignore[assignment]

    status, html_text = await client.get_html("https://example.test/listing")

    assert status is None
    assert 'data-item-id="1"' in html_text


async def test_get_profile_reviews_html_recovers_partial_html_after_goto_error():
    class FakePage:
        async def goto(self, *_args, **_kwargs):
            raise asyncio.TimeoutError("timeout")

        async def content(self):
            return (
                "<html><body>"
                '<div data-marker="review(0)">'
                '<p data-marker="review(0)/text-section/text">Очень хороший отзыв, всё сделано качественно.</p>'
                "</div>"
                '<script>"nextPage":"/web/6/user/abc123/ratings?limit=20"</script>'
                "</body></html>"
            )

    client = avito_module._PlaywrightBrowserClient(
        proxy={"server": "socks5://u:p@127.0.0.1:1080"},
        headless=True,
        timeout_ms=180_000,
        browser_name="chromium",
    )
    client._page = FakePage()  # type: ignore[assignment]

    html_text, clicks = await client.get_profile_reviews_html(
        "https://example.test/user/abc123/profile",
        max_reviews=20,
    )

    assert clicks == 0
    assert "/ratings" in html_text


def test_should_treat_as_proxy_error_for_winerror_and_timeout():
    from src.collectors.avito import _should_treat_as_proxy_error

    assert _should_treat_as_proxy_error(RuntimeError("[WinError 10054] connection reset"))
    assert _should_treat_as_proxy_error(asyncio.TimeoutError("timeout"))


async def test_collect_proxy_error_retries_immediately_and_rotates_proxy(monkeypatch):
    attempts: list[str] = []
    sleep_calls: list[float] = []

    class FakeBrowserClient:
        def __init__(self, **kwargs):
            self._proxy = kwargs.get("proxy")

        async def __aenter__(self):
            proxy_server = (self._proxy or {}).get("server", "direct")
            attempts.append(proxy_server)
            if ":1111" in proxy_server:
                raise RuntimeError("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    async def _fake_collect_with_client(self, keyword, url, client):  # noqa: ARG001
        return []

    async def _fake_sleep(delay=0, *args, **kwargs):  # noqa: ARG001
        sleep_calls.append(float(delay))

    monkeypatch.setattr(avito_module, "_PlaywrightBrowserClient", FakeBrowserClient)
    monkeypatch.setattr(AvitoCollector, "_collect_with_client", _fake_collect_with_client, raising=False)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(avito_module.settings, "avito_search_areas", "omsk")

    collector = AvitoCollector()
    collector._request_min_interval_s = 0.0
    collector._retry_jitter_max_s = 0.0  # disable jitter for deterministic test
    collector._proxy_max_rotations = 2
    collector._fetch_retries = 1
    collector._avito_proxy_urls = [
        "socks5://u:p@192.0.2.1:1111",
        "socks5://u:p@198.51.100.1:2222",
    ]
    collector._avito_proxy_index = 0

    result = await collector.collect("test")

    assert result == []
    assert len(attempts) == 2
    assert ":1111" in attempts[0]
    assert ":2222" in attempts[1]
    assert sleep_calls == [1.0]


# ---------------------------------------------------------------------------
# _ProxyStats scoring tests
# ---------------------------------------------------------------------------


def test_proxy_stats_score_neutral():
    stats = _ProxyStats()
    assert stats.score() == 0.0


def test_proxy_stats_score_positive():
    stats = _ProxyStats()
    for _ in range(10):
        stats.record_success()
    assert stats.score() > 0


def test_proxy_stats_score_negative_blocked():
    stats = _ProxyStats()
    for _ in range(10):
        stats.record_blocked()
    assert stats.score() < 0


def test_proxy_stats_blocked_worse_than_errors():
    blocked = _ProxyStats()
    for _ in range(5):
        blocked.record_blocked()
    errors = _ProxyStats()
    for _ in range(5):
        errors.record_error()
    assert blocked.score() < errors.score()


def test_proxy_stats_sliding_window_forgets_old():
    """Old failures are pushed out of the window by new successes."""
    from src.collectors.avito import _PROXY_STATS_WINDOW

    stats = _ProxyStats()
    # Fill window with blocks
    for _ in range(_PROXY_STATS_WINDOW):
        stats.record_blocked()
    assert stats.score() < 0

    # Now push successes to evict all blocks
    for _ in range(_PROXY_STATS_WINDOW):
        stats.record_success()
    assert stats.blocked_count == 0
    assert stats.score() > 0


def test_mark_proxy_failed_updates_stats():
    collector = AvitoCollector()
    collector._avito_proxy_urls = ["http://1", "http://2"]
    proxy = {"server": "http://1.2.3.4:8080"}

    collector._mark_proxy_failed(proxy, reason="blocked")

    proxy_id = collector._proxy_id(proxy)
    stats = collector._proxy_stats[proxy_id]
    assert stats.blocked_count == 1


def test_mark_proxy_success_updates_stats():
    collector = AvitoCollector()
    proxy = {"server": "http://1.2.3.4:8080"}

    collector._mark_proxy_success(proxy)

    proxy_id = collector._proxy_id(proxy)
    stats = collector._proxy_stats[proxy_id]
    assert stats.success_count == 1


def test_retry_wait_with_jitter_bounded():
    collector = AvitoCollector()
    collector._retry_jitter_max_s = 2.0
    collector._avito_proxy_urls = ["http://1"]

    for _ in range(20):
        wait = collector._retry_wait_s(attempt=1, proxy_error=False)
        assert wait <= 60.0


def test_retry_wait_no_jitter_when_zero():
    collector = AvitoCollector()
    collector._retry_jitter_max_s = 0.0
    collector._avito_proxy_urls = ["http://1"]

    wait = collector._retry_wait_s(attempt=1, proxy_error=False)
    assert wait == 2.0  # 2 * attempt = 2


@pytest.mark.asyncio
async def test_precheck_requires_avito_usable(monkeypatch):
    from src.collectors.avito_proxy_precheck import PrecheckSummary, ProxyPoolExhaustedError

    async def _fake_run_precheck(*args, **kwargs):
        return PrecheckSummary(
            total_tested=10,
            connectivity_ok=10,
            avito_ok=0,
            ranked_proxies=["http://p1:80", "http://p2:80"],
        )

    monkeypatch.setattr(avito_module.settings, "avito_precheck_enabled", True)
    monkeypatch.setattr(avito_module.settings, "avito_precheck_min_usable", 1)
    monkeypatch.setattr(avito_module.settings, "avito_precheck_min_avito_ok", 1)
    monkeypatch.setattr("src.collectors.avito_proxy_precheck.run_precheck", _fake_run_precheck)

    collector = AvitoCollector()
    collector._avito_proxy_urls = ["http://p1:80", "http://p2:80"]

    with pytest.raises(ProxyPoolExhaustedError, match="Avito-usable"):
        await collector.precheck_proxies()
