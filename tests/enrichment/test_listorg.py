"""Unit tests for the List-org registry checker."""

from unittest.mock import AsyncMock

import pytest

from src.enrichment.registries.base import CheckResult
from src.enrichment.registries.listorg import (
    ListOrgChecker,
    _assess_completeness,
    _extract_legal_status,
    _extract_company_name,
    _extract_fssp_signals,
    _extract_arbitr_signals,
    _extract_bankrupt_signals,
    _extract_sro_signals,
    _extract_zakupki_signals,
    _is_bot_page,
    _is_company_page,
    _extract_first_result_url,
    is_listorg_complete,
    map_listorg_to_registries,
)
from bs4 import BeautifulSoup


# ── Success case: full company page ──────────────────────────────────────

_FULL_PAGE_HTML = """
<html>
<head><title>ООО "Тестовая Компания" — List-Org</title></head>
<body>
<h1>ООО "Тестовая Компания"</h1>
<p>ИНН: 5501234567</p>
<p>ОГРН: 1155500001234</p>
<p>Статус: Действующее</p>
<p>Исполнительные производства ФССП: 2, сумма задолженности: 150 000 рублей</p>
<p>Арбитражных дел: 5</p>
<p>Участник закупок по 44-ФЗ</p>
<p>Членство в СРО НОСТРОЙ</p>
</body>
</html>
"""


def test_is_company_page():
    assert _is_company_page(_FULL_PAGE_HTML)
    assert not _is_company_page("<html><body>Nothing here</body></html>")


def test_is_company_page_rejects_search_results_page():
    html = """
    <html>
      <head><title>Список организаций с ИНН 5501234567</title></head>
      <body>
        <a href="/company/12345">ООО Тест</a>
        <div>ИНН 5501234567</div>
        <div>ОГРН 1155500001234</div>
      </body>
    </html>
    """
    assert _is_company_page(html) is False


def test_is_company_page_allows_company_page_with_search_links():
    html = """
    <html>
      <head><title>ООО "Тест", ИНН 5501234567</title></head>
      <body>
        <h1>ООО "Тест"</h1>
        <a href="/search?type=inn&val=5501234567">поиск по ИНН</a>
        <div>ИНН 5501234567</div>
        <div>ОГРН 1155500001234</div>
      </body>
    </html>
    """
    assert _is_company_page(html) is True


def test_extract_company_name():
    soup = BeautifulSoup(_FULL_PAGE_HTML, "html.parser")
    assert _extract_company_name(soup) == 'ООО "Тестовая Компания"'


def test_extract_legal_status_active():
    soup = BeautifulSoup(_FULL_PAGE_HTML, "html.parser")
    assert _extract_legal_status(soup) == "active"


def test_extract_legal_status_liquidated():
    html = '<html><body><p>Ликвидировано</p><p>ИНН: 123</p><p>ОГРН: 456</p></body></html>'
    soup = BeautifulSoup(html, "html.parser")
    assert _extract_legal_status(soup) == "liquidated"


def test_extract_legal_status_bankrupt():
    html = '<html><body><p>Банкрот</p><p>ИНН: 123</p><p>ОГРН: 456</p></body></html>'
    soup = BeautifulSoup(html, "html.parser")
    assert _extract_legal_status(soup) == "bankrupt"


def test_extract_fssp_signals():
    soup = BeautifulSoup(_FULL_PAGE_HTML, "html.parser")
    result = _extract_fssp_signals(soup)
    assert result["found"] is True
    assert result["details"]["total_debt_rub"] == 150000.0


def test_extract_arbitr_signals():
    soup = BeautifulSoup(_FULL_PAGE_HTML, "html.parser")
    result = _extract_arbitr_signals(soup)
    assert result["found"] is True
    assert result["details"]["total_count"] == 5


def test_extract_bankrupt_signals_no_bankruptcy():
    soup = BeautifulSoup(_FULL_PAGE_HTML, "html.parser")
    result = _extract_bankrupt_signals(soup, "active")
    assert result["found"] is False


def test_extract_bankrupt_signals_with_bankruptcy():
    html = '<html><body><p>Банкрот</p><p>ИНН: 123</p><p>ОГРН: 456</p></body></html>'
    soup = BeautifulSoup(html, "html.parser")
    result = _extract_bankrupt_signals(soup, "bankrupt")
    assert result["found"] is True
    assert result["status"] == "bankrupt"


def test_extract_zakupki_signals():
    soup = BeautifulSoup(_FULL_PAGE_HTML, "html.parser")
    result = _extract_zakupki_signals(soup)
    assert result["found"] is True


def test_extract_sro_signals():
    soup = BeautifulSoup(_FULL_PAGE_HTML, "html.parser")
    result = _extract_sro_signals(soup)
    assert result["found"] is True


# ── Bot page detection ────────────────────────────────────────────────────

def test_is_bot_page_detects_captcha():
    assert _is_bot_page("<html><body>Captcha required</body></html>")
    assert _is_bot_page("<html><body>Access Denied</body></html>")
    assert not _is_bot_page(_FULL_PAGE_HTML)


# ── Partial payload ──────────────────────────────────────────────────────

_PARTIAL_PAGE_HTML = """
<html><body>
<h1>ИП Иванов</h1>
<p>ИНН: 550100001234</p>
<p>ОГРНИП: 31455000012345</p>
<p>Статус: Действующее</p>
</body></html>
"""


def test_partial_payload_lower_completeness():
    soup = BeautifulSoup(_PARTIAL_PAGE_HTML, "html.parser")
    signals = {
        "legal_status": {"found": True},
        "fssp": {"found": False},
        "arbitr": {"found": False},
        "bankrupt": {"found": False},
        "zakupki": {"found": False},
        "sro": {"found": False},
    }
    score = _assess_completeness(signals)
    assert 0.5 < score <= 1.0  # All signals checked but not found


def test_full_completeness():
    signals = {
        "legal_status": {"found": True},
        "fssp": {"found": True},
        "arbitr": {"found": True},
        "bankrupt": {"found": True},
        "zakupki": {"found": True},
        "sro": {"found": True},
    }
    score = _assess_completeness(signals)
    assert score == 1.0


def test_no_legal_status_zero_completeness():
    signals = {"fssp": {"found": True}}
    score = _assess_completeness(signals)
    assert score == 0.0


# ── Not found ─────────────────────────────────────────────────────────────

def test_extract_first_result_url_none():
    html = '<html><body><p>No results</p></body></html>'
    assert _extract_first_result_url(html) is None


def test_extract_first_result_url_found():
    html = '<html><body><a href="/company/12345">Test</a></body></html>'
    url = _extract_first_result_url(html)
    assert url == "https://www.list-org.com/company/12345"


# ── map_listorg_to_registries ─────────────────────────────────────────────

def test_map_listorg_not_found():
    result = CheckResult(registry="listorg", found=False)
    assert map_listorg_to_registries(result) == {}


def test_map_listorg_full():
    result = CheckResult(
        registry="listorg",
        found=True,
        status="active",
        details={
            "signals": {
                "legal_status": {
                    "found": True,
                    "status": "active",
                    "details": {
                        "name": "Test",
                        "inn": "123",
                        "ogrn": "456",
                        "entity_type": "ЮЛ",
                        "director_name": "Иванов И.И.",
                        "director_position": "Генеральный директор",
                        "founders": ["Петров П.П."],
                    },
                },
                "fssp": {"found": True, "details": {"total_debt_rub": 50000, "executions_count": 1}},
                "arbitr": {"found": True, "details": {"total_count": 3, "active_cases_count": 1}},
                "bankrupt": {"found": False, "status": "mentioned", "details": {}},
                "zakupki": {"found": True, "details": {}},
                "sro": {"found": True, "active": True, "details": {}},
            },
            "completeness": 1.0,
        },
    )
    mapped = map_listorg_to_registries(result)

    assert "dadata_fns" in mapped
    assert mapped["dadata_fns"]["found"] is True
    assert mapped["dadata_fns"]["status"] == "ACTIVE"
    assert mapped["dadata_fns"]["details"]["director_name"] == "Иванов И.И."
    assert mapped["dadata_fns"]["details"]["founders"] == ["Петров П.П."]

    assert "fssp" in mapped
    assert mapped["fssp"]["found"] is True

    assert "kad_arbitr" in mapped
    assert mapped["kad_arbitr"]["found"] is True

    assert "nostroy" in mapped
    assert mapped["nostroy"]["found"] is True
    assert mapped["nostroy"]["status"] == "active"

    assert "eis_zakupki" in mapped
    assert mapped["eis_zakupki"]["found"] is True


def test_map_listorg_liquidated_status():
    result = CheckResult(
        registry="listorg",
        found=True,
        status="liquidated",
        details={
            "signals": {
                "legal_status": {
                    "found": True,
                    "status": "ликвидировано",
                    "details": {"name": "Test"},
                },
            },
            "completeness": 0.3,
        },
    )
    mapped = map_listorg_to_registries(result)
    assert mapped["dadata_fns"]["status"] == "liquidated"


# ── is_listorg_complete ──────────────────────────────────────────────────

def test_is_listorg_complete_true():
    result = CheckResult(
        registry="listorg", found=True, details={"completeness": 0.8}
    )
    assert is_listorg_complete(result) is True


def test_is_listorg_complete_false_low():
    result = CheckResult(
        registry="listorg", found=True, details={"completeness": 0.4}
    )
    assert is_listorg_complete(result) is False


def test_is_listorg_complete_false_not_found():
    result = CheckResult(registry="listorg", found=False)
    assert is_listorg_complete(result) is False


# ── Parse regression test ────────────────────────────────────────────────

def test_parse_regression_full_page():
    """Ensure the checker's internal _parse_page produces correct structure."""
    checker = ListOrgChecker()
    result = checker._parse_page(
        _FULL_PAGE_HTML,
        "5501234567",
        search_type="inn",
        name_match_score=None,
    )

    assert result.found is True
    assert result.status == "active"
    assert result.details["name"] == 'ООО "Тестовая Компания"'
    assert result.details["signals"]["fssp"]["found"] is True
    assert result.details["signals"]["arbitr"]["found"] is True
    assert result.details["signals"]["sro"]["found"] is True
    assert result.details["completeness"] == 1.0


@pytest.mark.asyncio
async def test_name_lookup_enabled_uses_name_search_type():
    checker = ListOrgChecker()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("src.enrichment.registries.listorg.settings.listorg_enabled", True)
        mp.setattr("src.enrichment.registries.listorg.settings.listorg_allow_name_lookup", True)
        checker._fetch_page = AsyncMock(return_value=(_FULL_PAGE_HTML, 1.0))

        result = await checker.check(inn=None, ogrn=None, name="Тестовая Компания")

    assert result.found is True
    checker._fetch_page.assert_awaited_once()
    args, kwargs = checker._fetch_page.await_args
    assert kwargs["search_type"] == "name"
    assert "Тестовая Компания" in args[0]


@pytest.mark.asyncio
async def test_fetch_page_not_found_returns_tuple(monkeypatch):
    checker = ListOrgChecker()

    class _Resp:
        status_code = 200
        text = "<html><body><p>No results</p></body></html>"

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *args, **kwargs):  # noqa: ARG002
            return _Resp()

    monkeypatch.setattr("src.enrichment.registries.listorg.httpx.AsyncClient", lambda **kwargs: _Client())

    result = await checker._fetch_page(
        "несуществующая компания",
        search_type="name",
        match_name="несуществующая компания",
    )

    assert isinstance(result, tuple)
    html, score = result
    assert isinstance(html, str)
    assert score is None


@pytest.mark.asyncio
async def test_fetch_page_inn_result_returns_tuple(monkeypatch):
    checker = ListOrgChecker()

    search_html = """
    <html><body>
      <a href="/company/12345">ООО Тест</a>
    </body></html>
    """
    company_html = _FULL_PAGE_HTML

    class _Resp:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, *args, **kwargs):  # noqa: ARG002
            if "search" in str(url):
                return _Resp(200, search_html)
            return _Resp(200, company_html)

    monkeypatch.setattr("src.enrichment.registries.listorg.httpx.AsyncClient", lambda **kwargs: _Client())

    result = await checker._fetch_page(
        "5501234567",
        search_type="inn",
        match_name="",
    )

    assert isinstance(result, tuple)
    html, score = result
    assert "Тестовая Компания" in html
    assert score is None
