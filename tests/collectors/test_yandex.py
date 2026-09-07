from datetime import UTC

from src.collectors.base import RawCompany, RawReview
from src.collectors.yandex import YandexCollector


def test_merge_state_into_dom_companies_keeps_dom_reviews_and_enriches_fields():
    collector = YandexCollector()
    dom_review = RawReview(source="yandex", text="Отлично", rating=5.0, author="Иван")
    dom_company = RawCompany(
        source="yandex",
        source_id="1001",
        source_link="https://yandex.ru/maps/org/company_a/1001/",
        name_raw="Компания А",
        phones=["+7 111 111-11-11"],
        addresses=["Москва, улица 1"],
        contacts_json={"websites": ["https://dom.example"], "tags": ["dom"]},
        reviews=[dom_review],
        reviews_count=1,
    )
    state_company = RawCompany(
        source="yandex",
        source_id="1001",
        source_link="https://yandex.ru/maps/org/company_a/1001/?ll=37,55",
        name_raw="Компания А",
        phones=["+7 222 222-22-22"],
        addresses=["Москва, улица 2"],
        contacts_json={"websites": ["https://state.example"], "categories": ["Бурение"]},
        reviews_count=27,
    )

    merged = collector._merge_state_into_dom_companies([dom_company], [state_company])

    assert len(merged) == 1
    company = merged[0]
    assert company.reviews == [dom_review]
    assert company.reviews_count == 27
    assert company.phones == ["+7 111 111-11-11", "+7 222 222-22-22"]
    assert company.addresses == ["Москва, улица 1", "Москва, улица 2"]
    assert company.contacts_json["websites"] == ["https://dom.example", "https://state.example"]
    assert company.contacts_json["categories"] == ["Бурение"]


def test_raw_review_from_dom_item_parses_fields():
    collector = YandexCollector()
    row = {
        "text": " Отличная работа ",
        "author": " Алёнушка ",
        "rating": "Оценка 4,7 Из 5",
        "review_date": "2026-01-26T06:30:01.289Z",
        "source_link": "/maps/org/nedra/1316011917/reviews/",
    }

    review = collector._raw_review_from_dom_item(row, "https://yandex.ru/maps/org/nedra/1316011917/")

    assert review is not None
    assert review.text == "Отличная работа"
    assert review.author == "Алёнушка"
    assert review.rating == 4.7
    assert review.review_date is not None
    assert review.review_date.tzinfo == UTC
    assert review.source_link == "https://yandex.ru/maps/org/nedra/1316011917/reviews/"


def test_raw_review_from_dom_item_handles_missing_fields():
    collector = YandexCollector()
    row = {
        "text": "Без рейтинга и даты",
        "author": "",
        "rating": "",
        "review_date": "",
        "source_link": "",
    }

    review = collector._raw_review_from_dom_item(row, "https://yandex.ru/maps/org/test/123/")

    assert review is not None
    assert review.text == "Без рейтинга и даты"
    assert review.author is None
    assert review.rating is None
    assert review.review_date is None
    assert review.source_link == "https://yandex.ru/maps/org/test/123/"


def test_resolve_keyword_for_url_uses_url_text_for_broken_keyword():
    collector = YandexCollector()
    url = "https://yandex.ru/maps/?text=%D0%B1%D1%83%D1%80%D0%B5%D0%BD%D0%B8%D0%B5+%D1%81%D0%BA%D0%B2%D0%B0%D0%B6%D0%B8%D0%BD+%D0%9E%D0%BC%D1%81%D0%BA"

    resolved = collector._resolve_keyword_for_url("??????? ??????? ????", url)

    assert resolved == "бурение скважин Омск"


def test_resolve_keyword_for_url_keeps_valid_keyword():
    collector = YandexCollector()
    url = "https://yandex.ru/maps/?text=%D0%B1%D1%83%D1%80%D0%B5%D0%BD%D0%B8%D0%B5+%D1%81%D0%BA%D0%B2%D0%B0%D0%B6%D0%B8%D0%BD+%D0%9E%D0%BC%D1%81%D0%BA"

    resolved = collector._resolve_keyword_for_url("бурение скважин Омск", url)

    assert resolved == "бурение скважин Омск"


async def test_goto_with_fallback_uses_commit_after_timeout():
    collector = YandexCollector()

    class DummyResponse:
        status = 200

    class DummyPage:
        def __init__(self):
            self.calls = []

        async def goto(self, url, *, wait_until, timeout):
            self.calls.append((url, wait_until, timeout))
            if wait_until == "domcontentloaded":
                raise RuntimeError("Timeout 45000ms exceeded")
            return DummyResponse()

    page = DummyPage()
    response = await collector._goto_with_fallback(
        page,
        "https://yandex.ru/maps/?text=test",
        timeout_ms=45_000,
        purpose="listing",
    )

    assert response.status == 200
    assert page.calls == [
        ("https://yandex.ru/maps/?text=test", "domcontentloaded", 45_000),
        ("https://yandex.ru/maps/?text=test", "commit", 15_000),
    ]


async def test_goto_with_fallback_keeps_primary_success():
    collector = YandexCollector()

    class DummyResponse:
        status = 200

    class DummyPage:
        def __init__(self):
            self.calls = []

        async def goto(self, url, *, wait_until, timeout):
            self.calls.append((url, wait_until, timeout))
            return DummyResponse()

    page = DummyPage()
    response = await collector._goto_with_fallback(
        page,
        "https://yandex.ru/maps/?text=test",
        timeout_ms=30_000,
        purpose="reviews_direct",
    )

    assert response.status == 200
    assert page.calls == [
        ("https://yandex.ru/maps/?text=test", "domcontentloaded", 30_000),
    ]


async def test_collect_reviews_for_listing_skips_company_after_tab_retries():
    collector = YandexCollector()
    first = RawCompany(
        source="yandex",
        source_id="1",
        source_link="https://yandex.ru/maps/org/a/1/",
        name_raw="A",
        reviews_count=1,
    )
    second = RawCompany(
        source="yandex",
        source_id="2",
        source_link="https://yandex.ru/maps/org/b/2/",
        name_raw="B",
        reviews_count=1,
    )

    class DummyPage:
        url = "https://yandex.ru/maps/"

    async def fake_open_company(page, company, preferred_index):
        return True

    async def fake_open_tab(page, company):
        return company.source_id == "2"

    async def fake_scroll(page):
        return None

    async def fake_extract(page, company_link):
        return [RawReview(source="yandex", text="ok", rating=5.0, author="User")]

    collector._open_company_from_listing = fake_open_company
    collector._open_reviews_tab_with_retry = fake_open_tab
    collector._scroll_reviews_until_end = fake_scroll
    collector._extract_reviews_from_open_company = fake_extract

    await collector._collect_reviews_for_listing(DummyPage(), [first, second])

    assert first.reviews == []
    assert second.reviews_count == 1
    assert len(second.reviews) == 1


async def test_collect_reviews_for_listing_handles_zero_reviews_for_second_company():
    collector = YandexCollector()
    first = RawCompany(
        source="yandex",
        source_id="1",
        source_link="https://yandex.ru/maps/org/a/1/",
        name_raw="A",
        reviews_count=1,
    )
    second = RawCompany(
        source="yandex",
        source_id="2",
        source_link="https://yandex.ru/maps/org/b/2/",
        name_raw="B",
        reviews_count=0,
    )

    class DummyPage:
        url = "https://yandex.ru/maps/"

    async def fake_open_company(page, company, preferred_index):
        return True

    async def fake_open_tab(page, company):
        return True

    async def fake_scroll(page):
        return None

    async def fake_extract(page, company_link):
        if "a/1" in (company_link or ""):
            return [RawReview(source="yandex", text="Есть отзыв", rating=4.0, author="User")]
        return []

    collector._open_company_from_listing = fake_open_company
    collector._open_reviews_tab_with_retry = fake_open_tab
    collector._scroll_reviews_until_end = fake_scroll
    collector._extract_reviews_from_open_company = fake_extract

    await collector._collect_reviews_for_listing(DummyPage(), [first, second])

    assert len(first.reviews) == 1
    assert first.reviews_count == 1
    assert second.reviews == []
    assert second.reviews_count == 0


async def test_collect_reviews_for_listing_skips_company_with_zero_reviews_count():
    collector = YandexCollector()
    first = RawCompany(
        source="yandex",
        source_id="1",
        source_link="https://yandex.ru/maps/org/a/1/",
        name_raw="A",
        reviews_count=0,
    )
    second = RawCompany(
        source="yandex",
        source_id="2",
        source_link="https://yandex.ru/maps/org/b/2/",
        name_raw="B",
        reviews_count=1,
    )

    class DummyPage:
        url = "https://yandex.ru/maps/"

    opened_ids: list[str] = []

    async def fake_open_company(page, company, preferred_index):
        opened_ids.append(company.source_id or "")
        return True

    async def fake_open_tab(page, company):
        return True

    async def fake_scroll(page):
        return None

    async def fake_extract(page, company_link):
        return [RawReview(source="yandex", text="ok", rating=5.0, author="User")]

    collector._open_company_from_listing = fake_open_company
    collector._open_reviews_tab_with_retry = fake_open_tab
    collector._scroll_reviews_until_end = fake_scroll
    collector._extract_reviews_from_open_company = fake_extract

    await collector._collect_reviews_for_listing(DummyPage(), [first, second])

    # With relaxed gating, first company (reviews_count=0 but has source_link)
    # is now attempted too, so both should be opened.
    assert opened_ids == ["1", "2"]
    assert len(first.reviews) == 1  # Fetched via listing-click
    assert second.reviews_count == 1
    assert len(second.reviews) == 1


async def test_collect_reviews_for_listing_skips_company_with_unknown_reviews_count():
    """With relaxed gating, companies with source_link are still attempted
    even when reviews_count is None."""
    collector = YandexCollector()
    first = RawCompany(
        source="yandex",
        source_id="1",
        source_link="https://yandex.ru/maps/org/a/1/",
        name_raw="A",
        reviews_count=None,
    )
    second = RawCompany(
        source="yandex",
        source_id="2",
        source_link="https://yandex.ru/maps/org/b/2/",
        name_raw="B",
        reviews_count=2,
    )

    class DummyPage:
        url = "https://yandex.ru/maps/"

    opened_ids: list[str] = []

    async def fake_open_company(page, company, preferred_index):
        opened_ids.append(company.source_id or "")
        return True

    async def fake_open_tab(page, company):
        return True

    async def fake_scroll(page):
        return None

    async def fake_extract(page, company_link):
        return [RawReview(source="yandex", text="ok", rating=5.0, author="User")]

    collector._open_company_from_listing = fake_open_company
    collector._open_reviews_tab_with_retry = fake_open_tab
    collector._scroll_reviews_until_end = fake_scroll
    collector._extract_reviews_from_open_company = fake_extract

    await collector._collect_reviews_for_listing(DummyPage(), [first, second])

    # Both attempted since both have source_link (relaxed gating)
    assert opened_ids == ["1", "2"]
    assert len(first.reviews) == 1
    assert len(second.reviews) == 1


async def test_open_reviews_tab_with_retry_accepts_empty_reviews_state():
    collector = YandexCollector()
    company = RawCompany(source="yandex", source_id="1", name_raw="A")

    class DummyPage:
        url = "https://yandex.ru/maps/"

    async def tab_not_open(page):
        return False

    async def empty_state(page):
        return True

    collector._is_reviews_tab_open = tab_not_open
    collector._is_reviews_empty_state = empty_state

    opened = await collector._open_reviews_tab_with_retry(DummyPage(), company)

    assert opened is True


async def test_is_reviews_tab_open_accepts_generic_selected_tab():
    collector = YandexCollector()

    class DummyPage:
        url = "https://yandex.ru/maps/"

        async def query_selector(self, selector):
            return None

        async def evaluate(self, script):
            return True

    assert await collector._is_reviews_tab_open(DummyPage()) is True


async def test_is_reviews_empty_state_detects_empty_tab_markup():
    collector = YandexCollector()

    class DummyPage:
        async def query_selector(self, selector):
            if selector == ".card-reviews-view._empty-tab":
                return object()
            return None

        async def evaluate(self, script):
            return False

    assert await collector._is_reviews_empty_state(DummyPage()) is True
