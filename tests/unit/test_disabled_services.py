"""Tests for FSSP and DDG disable flags.

Verifies:
- FSSP checker is not called when ENABLE_FSSP_CHECK=false
- DDG search is not called when ENABLE_DDG_SEARCH=false
- Enrichment completes with list-org data when both are disabled
- Stable stubs are placed correctly
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.enrichment.enricher import (
    Enricher,
    _FSSP_CHECKER,
    _FSSP_DISABLED_STUB,
    _LEGACY_CHECKERS_ALWAYS,
)
from src.enrichment.registries.base import CheckResult
from src.enrichment.review_searcher import ReviewSearcher


# ── FSSP tests ──────────────────────────────────────────────────────────────


class TestFsspDisabled:
    @pytest.mark.asyncio
    async def test_fssp_not_called_when_disabled(self):
        """When ENABLE_FSSP_CHECK=false, FsspChecker.safe_check is never called."""
        enricher = Enricher()

        with (
            patch("src.enrichment.enricher.settings") as mock_settings,
            patch.object(_FSSP_CHECKER, "safe_check", new_callable=AsyncMock) as fssp_mock,
        ):
            mock_settings.enable_fssp_check = False
            mock_settings.listorg_enabled = False
            mock_settings.listorg_primary = False
            mock_settings.listorg_fallback_enabled = False

            # Mock all legacy checkers
            for checker in _LEGACY_CHECKERS_ALWAYS:
                checker.safe_check = AsyncMock(
                    return_value=CheckResult(
                        registry=checker.registry_name,
                        found=False,
                        details={},
                    )
                )

            checks = await enricher._run_registry_checks(
                inn="1234567890", ogrn=None, name="Test", raw_id="test-id"
            )

            fssp_mock.assert_not_called()
            assert "fssp" in checks
            assert checks["fssp"]["found"] is False
            assert checks["fssp"]["error"] == "disabled_by_config"

    @pytest.mark.asyncio
    async def test_fssp_called_when_enabled(self):
        """When ENABLE_FSSP_CHECK=true, FsspChecker.safe_check IS called."""
        enricher = Enricher()

        with (
            patch("src.enrichment.enricher.settings") as mock_settings,
            patch.object(
                _FSSP_CHECKER,
                "safe_check",
                new_callable=AsyncMock,
                return_value=CheckResult(registry="fssp", found=False, details={}),
            ) as fssp_mock,
        ):
            mock_settings.enable_fssp_check = True
            mock_settings.listorg_enabled = False
            mock_settings.listorg_primary = False
            mock_settings.listorg_fallback_enabled = False

            for checker in _LEGACY_CHECKERS_ALWAYS:
                checker.safe_check = AsyncMock(
                    return_value=CheckResult(
                        registry=checker.registry_name,
                        found=False,
                        details={},
                    )
                )

            checks = await enricher._run_registry_checks(
                inn="1234567890", ogrn=None, name="Test", raw_id="test-id"
            )

            fssp_mock.assert_called_once()
            assert "fssp" in checks

    @pytest.mark.asyncio
    async def test_fssp_stub_not_overwritten_by_listorg(self):
        """When FSSP disabled but list-org provides fssp data, list-org data is kept."""
        enricher = Enricher()

        listorg_result = CheckResult(
            registry="listorg",
            found=True,
            details={
                "completeness": 0.8,
                "signals": {"fssp": {"found": True, "total_debt": 50000}},
            },
        )

        with (
            patch("src.enrichment.enricher.settings") as mock_settings,
            patch("src.enrichment.enricher._listorg_checker") as mock_listorg,
            patch.object(_FSSP_CHECKER, "safe_check", new_callable=AsyncMock) as fssp_mock,
        ):
            mock_settings.enable_fssp_check = False
            mock_settings.listorg_enabled = True
            mock_settings.listorg_primary = True
            mock_settings.listorg_fallback_enabled = False
            mock_listorg.safe_check = AsyncMock(return_value=listorg_result)

            # Mock map_listorg_to_registries to return fssp data
            with patch("src.enrichment.enricher.map_listorg_to_registries") as mock_map:
                mock_map.return_value = {
                    "fssp": {"registry": "fssp", "found": True, "details": {"total_debt": 50000}},
                }
                with patch("src.enrichment.enricher.is_listorg_complete", return_value=True):
                    checks = await enricher._run_registry_checks(
                        inn="1234567890", ogrn=None, name="Test", raw_id="test-id"
                    )

            # list-org provided fssp data, stub should NOT overwrite it
            fssp_mock.assert_not_called()
            assert checks["fssp"]["found"] is True


def test_fssp_disabled_stub_structure():
    """Verify stub has all required keys."""
    assert _FSSP_DISABLED_STUB["registry"] == "fssp"
    assert _FSSP_DISABLED_STUB["found"] is False
    assert _FSSP_DISABLED_STUB["error"] == "disabled_by_config"
    assert _FSSP_DISABLED_STUB["details"] == {}


# ── DDG tests ───────────────────────────────────────────────────────────────


class TestDdgDisabled:
    @pytest.mark.asyncio
    async def test_ddg_not_called_when_disabled(self):
        """When ENABLE_DDG_SEARCH=false, _ddg_search is never invoked."""
        searcher = ReviewSearcher()

        with (
            patch("src.enrichment.review_searcher.settings") as mock_settings,
            patch.object(searcher, "_ddg_search_safe", new_callable=AsyncMock) as ddg_mock,
            patch("src.enrichment.review_searcher.require_proxy_pool"),
        ):
            mock_settings.enable_ddg_search = False
            mock_settings.serpapi_key = ""

            result = await searcher._search_urls("test query")

            ddg_mock.assert_not_called()
            assert result == []

    @pytest.mark.asyncio
    async def test_ddg_disabled_uses_serpapi_if_key_present(self):
        """When DDG disabled but SERPAPI_KEY set, falls back to SerpAPI."""
        searcher = ReviewSearcher()

        with (
            patch("src.enrichment.review_searcher.settings") as mock_settings,
            patch.object(searcher, "_ddg_search_safe", new_callable=AsyncMock) as ddg_mock,
            patch.object(
                searcher,
                "_serpapi_search",
                new_callable=AsyncMock,
                return_value=["https://example.com"],
            ) as serpapi_mock,
            patch("src.enrichment.review_searcher.require_proxy_pool"),
        ):
            mock_settings.enable_ddg_search = False
            mock_settings.serpapi_key = "test-key"

            result = await searcher._search_urls("test query")

            ddg_mock.assert_not_called()
            serpapi_mock.assert_called_once_with("test query")
            assert result == ["https://example.com"]

    @pytest.mark.asyncio
    async def test_ddg_called_when_enabled(self):
        """When ENABLE_DDG_SEARCH=true, DDG is attempted."""
        searcher = ReviewSearcher()

        with (
            patch("src.enrichment.review_searcher.settings") as mock_settings,
            patch.object(
                searcher,
                "_ddg_search_safe",
                new_callable=AsyncMock,
                return_value=["https://flamp.ru/firm/123"],
            ) as ddg_mock,
            patch("src.enrichment.review_searcher.require_proxy_pool"),
        ):
            mock_settings.enable_ddg_search = True
            mock_settings.serpapi_key = ""

            result = await searcher._search_urls("test query")

            ddg_mock.assert_called_once()
            assert result == ["https://flamp.ru/firm/123"]

    @pytest.mark.asyncio
    async def test_ddg_disabled_returns_empty_fast(self):
        """When DDG disabled and no SerpAPI, return empty without delay."""
        searcher = ReviewSearcher()

        with (
            patch("src.enrichment.review_searcher.settings") as mock_settings,
            patch("src.enrichment.review_searcher.require_proxy_pool"),
        ):
            mock_settings.enable_ddg_search = False
            mock_settings.serpapi_key = ""

            import time

            start = time.monotonic()
            result = await searcher._search_urls("test query")
            elapsed = time.monotonic() - start

            assert result == []
            assert elapsed < 1.0  # Should be near-instant
