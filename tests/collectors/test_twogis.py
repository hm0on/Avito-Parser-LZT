"""UI-only tests for the 2GIS collector."""

import json
from unittest.mock import AsyncMock, patch

from bs4 import BeautifulSoup
import pytest

import src.collectors.twogis as twogis_module
from src.collectors.base import RawCompany
from src.collectors.twogis import (
    TwoGisCollector,
    TwoGisProxyAuthError,
    _extract_addresses,
    _extract_firm_id,
    _extract_phones,
    _extract_rating_info,
    _extract_tax_ids,
    _extract_reviews_from_initial_state,
    _extract_reviews_from_soup,
    _is_proxy_auth_error,
    _rewrite_proxy_scheme,
)


async def test_collect_skips_without_twogis_proxies(monkeypatch):
    monkeypatch.setattr(twogis_module.twogis_proxy_manager, "_proxies", [])

    collector = TwoGisCollector()

    with pytest.raises(RuntimeError, match="Proxy is required for 2GIS collection"):
        await collector.collect("drain cleaning")


async def test_collect_uses_ui_results(monkeypatch):
    monkeypatch.setattr(
        twogis_module.twogis_proxy_manager,
        "_proxies",
        ["http://user:pass@127.0.0.1:8080"],
    )
    monkeypatch.setattr(twogis_module.settings, "twogis_search_areas", "plumber")
    expected = [RawCompany(source="2gis", source_id="123", name_raw="Test firm")]
    collector = TwoGisCollector()
    collector._collect_via_ui = AsyncMock(return_value=expected)

    result = await collector.collect("plumber")

    collector._collect_via_ui.assert_awaited_once_with("plumber")
    assert result == expected


def test_extract_firm_id():
    assert _extract_firm_id("https://2gis.ru/omsk/firm/70000001012345") == "70000001012345"
    assert _extract_firm_id("https://2gis.ru/omsk/search/test") is None


def test_extract_rating_info_prefers_ld_json():
    soup = BeautifulSoup(
        """
        <html>
          <head>
            <script type="application/ld+json">
              {"aggregateRating": {"ratingValue": "4.7", "reviewCount": "12"}}
            </script>
          </head>
        </html>
        """,
        "html.parser",
    )

    rating, count = _extract_rating_info(soup)

    assert rating == 4.7
    assert count == 12


def test_extract_phones_and_addresses():
    soup = BeautifulSoup(
        """
        <html>
          <head>
            <script type="application/ld+json">
              {
                "address": {
                  "addressLocality": "Omsk",
                  "streetAddress": "Lenina 1"
                }
              }
            </script>
          </head>
          <body>
            <a href="tel:+7 (913) 123-45-67">Call</a>
            <a href="tel:8 (3812) 55-44-33">Office</a>
            <a href="/geo/70030076146048957">Lenina 1</a>
          </body>
        </html>
        """,
        "html.parser",
    )

    assert _extract_phones(soup) == ["+79131234567", "+73812554433"]
    assert "Omsk, Lenina 1" in _extract_addresses(soup)


def test_extract_tax_ids_from_html_and_ld_json():
    soup = BeautifulSoup(
        """
        <html>
          <head>
            <script type="application/ld+json">
              {"@type": "Organization", "name": "Тест", "taxID": "5501234567"}
            </script>
          </head>
          <body>
            <div>ОГРН: 1155500001234</div>
          </body>
        </html>
        """,
        "html.parser",
    )
    html = str(soup)

    inn, ogrn = _extract_tax_ids(soup, html)

    assert inn == "5501234567"
    assert ogrn == "1155500001234"


def test_extract_reviews_from_soup_deduplicates():
    soup = BeautifulSoup(
        """
        <html><body>
          <div class="reviewsItem">First review text is long enough to keep.</div>
          <div class="reviewsItem">First review text is long enough to keep.</div>
          <div class="reviewsItem">Second review text is also long enough to keep.</div>
        </body></html>
        """,
        "html.parser",
    )

    reviews = _extract_reviews_from_soup(soup, source_link="https://2gis.ru/omsk/firm/1")

    assert len(reviews) == 2
    assert {review.source_link for review in reviews} == {"https://2gis.ru/omsk/firm/1"}


def test_extract_reviews_from_initial_state_filters_by_source_id():
    state = {
        "data": {
            "review": {
                "r1": {
                    "data": {
                        "text": "Отличная компания, всё сделали аккуратно и в срок.",
                        "rating": 5,
                        "date_created": "2025-12-10T12:00:00+07:00",
                        "user": {"name": "Иван"},
                        "object": {"id": "123"},
                    }
                },
                "r2": {
                    "data": {
                        "text": "Этот отзыв от другой компании и должен быть отфильтрован.",
                        "rating": 4,
                        "date_created": "2025-12-11T12:00:00+07:00",
                        "user": {"name": "Петр"},
                        "object": {"id": "999"},
                    }
                },
            }
        }
    }
    raw = json.dumps(state, ensure_ascii=True).replace("\\", "\\\\").replace("'", "\\'")
    html = f"<script>var initialState = JSON.parse('{raw}');</script>"

    reviews = _extract_reviews_from_initial_state(
        html,
        source_id="123",
        source_link="https://2gis.ru/omsk/firm/123",
    )

    assert len(reviews) == 1
    assert reviews[0].author == "Иван"
    assert reviews[0].rating == 5.0
    assert "Отличная компания" in reviews[0].text


def test_extract_reviews_from_initial_state_fixes_mojibake():
    broken_text = "Тестовый отзыв с русским текстом и деталями.".encode("utf-8").decode("latin1")
    broken_author = "Мария".encode("utf-8").decode("latin1")
    state = {
        "data": {
            "review": {
                "r1": {
                    "data": {
                        "text": broken_text,
                        "rating": 5,
                        "date_created": "2025-12-10T12:00:00+07:00",
                        "user": {"name": broken_author},
                        "object": {"id": "123"},
                    }
                },
            }
        }
    }
    raw = json.dumps(state, ensure_ascii=False).replace("\\", "\\\\").replace("'", "\\'")
    html = f"<script>var initialState = JSON.parse('{raw}');</script>"

    reviews = _extract_reviews_from_initial_state(
        html,
        source_id="123",
        source_link="https://2gis.ru/omsk/firm/123",
    )

    assert len(reviews) == 1
    assert "Ð" not in reviews[0].text
    assert reviews[0].author == "Мария"


# ── Proxy auth error classification ──────────────────────────────────────


class TestIsProxyAuthError:
    def test_detects_err_proxy_auth_unsupported(self):
        assert _is_proxy_auth_error("Page.goto: net::ERR_PROXY_AUTH_UNSUPPORTED") is True

    def test_detects_407(self):
        assert _is_proxy_auth_error("CONNECT tunnel failed, response 407") is True

    def test_detects_proxy_authentication_required(self):
        assert _is_proxy_auth_error("Proxy Authentication Required") is True

    def test_detects_tunnel_failed(self):
        assert _is_proxy_auth_error("net::ERR_TUNNEL_CONNECTION_FAILED") is True

    def test_not_auth_error_for_timeout(self):
        assert _is_proxy_auth_error("Timeout 30000ms exceeded") is False

    def test_not_auth_error_for_captcha(self):
        assert _is_proxy_auth_error("captcha.2gis.ru detected") is False

    def test_empty_string(self):
        assert _is_proxy_auth_error("") is False

    def test_none(self):
        assert _is_proxy_auth_error(None) is False


# ── Fail-fast on proxy auth ──────────────────────────────────────────────


async def test_collect_via_ui_fail_fast_on_proxy_auth(monkeypatch):
    """When all proxies fail with auth errors, _collect_via_ui raises TwoGisProxyAuthError."""
    monkeypatch.setattr(
        twogis_module.twogis_proxy_manager,
        "_proxies",
        ["http://bad:bad@127.0.0.1:8080", "http://bad:bad@127.0.0.1:8081", "http://bad:bad@127.0.0.1:8082"],
    )
    monkeypatch.setattr(twogis_module.settings, "twogis_max_proxy_rotations", 5)

    collector = TwoGisCollector()
    collector._collect_via_ui_once_classified = AsyncMock(return_value=([], "proxy_auth"))

    with pytest.raises(TwoGisProxyAuthError, match="proxy pool unauthorized"):
        await collector._collect_via_ui("test keyword")


async def test_collect_via_ui_no_fail_fast_on_non_auth_errors(monkeypatch):
    """Non-auth errors don't trigger fail-fast, just return empty."""
    monkeypatch.setattr(
        twogis_module.twogis_proxy_manager,
        "_proxies",
        ["http://p1:p@127.0.0.1:8080"],
    )
    monkeypatch.setattr(twogis_module.settings, "twogis_max_proxy_rotations", 1)

    collector = TwoGisCollector()
    collector._collect_via_ui_once_classified = AsyncMock(return_value=([], "blocked"))

    result = await collector._collect_via_ui("test keyword")
    assert result == []


# ── Precheck ─────────────────────────────────────────────────────────────


async def test_precheck_raises_when_all_proxies_fail(monkeypatch):
    """precheck_proxies raises ProxyPoolExhaustedError when no proxy passes."""
    import httpx

    from src.collectors.avito_proxy_precheck import ProxyPoolExhaustedError

    monkeypatch.setattr(
        twogis_module.twogis_proxy_manager,
        "_proxies",
        ["http://bad:bad@127.0.0.1:9090"],
    )

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.ProxyError("407 Proxy Authentication Required"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    collector = TwoGisCollector()
    with patch("src.collectors.twogis.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ProxyPoolExhaustedError, match="proxy pool unauthorized"):
            await collector.precheck_proxies()


async def test_precheck_passes_when_proxy_ok(monkeypatch):
    """precheck_proxies returns summary when at least one proxy works."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(
        twogis_module.twogis_proxy_manager,
        "_proxies",
        ["http://good:good@127.0.0.1:9090"],
    )

    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    collector = TwoGisCollector()
    with patch("src.collectors.twogis.httpx.AsyncClient", return_value=mock_client):
        summary = await collector.precheck_proxies()

    assert summary.total_tested == 1
    assert summary.avito_ok == 1


# ── Proxy scheme rewrite ─────────────────────────────────────────────────


class TestRewriteProxyScheme:
    def test_http_to_socks5(self):
        result = _rewrite_proxy_scheme("http://user:pass@1.2.3.4:8080", "socks5")
        assert result == "socks5://user:pass@1.2.3.4:8080"

    def test_socks5_to_socks5_noop(self):
        url = "socks5://user:pass@1.2.3.4:8080"
        assert _rewrite_proxy_scheme(url, "socks5") == url

    def test_preserves_url_encoded_credentials(self):
        result = _rewrite_proxy_scheme("http://us%40er:p%23ss@host:1234", "socks5")
        assert result.startswith("socks5://")
        assert "@host:1234" in result
        # URL-encoded chars should be preserved
        assert "%40" in result or "@" in result.split("@")[0]

    def test_empty_scheme_returns_original(self):
        url = "http://user:pass@host:1234"
        assert _rewrite_proxy_scheme(url, "") == url

    def test_none_url_returns_none(self):
        assert _rewrite_proxy_scheme(None, "socks5") is None

    def test_empty_url_returns_empty(self):
        assert _rewrite_proxy_scheme("", "socks5") == ""
