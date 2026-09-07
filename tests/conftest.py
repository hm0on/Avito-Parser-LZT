"""Shared pytest fixtures for the Avito Parser test suite."""

import asyncio
import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_json_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace asyncio.sleep with an instant no-op."""
    async def _instant(_delay=0, **_kw):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant)
    return _instant


@pytest.fixture
def avito_listing_html() -> str:
    return load_fixture("avito_listing.html")


@pytest.fixture
def avito_profile_html() -> str:
    return load_fixture("avito_profile.html")


@pytest.fixture
def twogis_catalog_json() -> dict:
    return load_json_fixture("twogis_catalog_response.json")


@pytest.fixture
def twogis_reviews_json() -> dict:
    return load_json_fixture("twogis_reviews_response.json")


@pytest.fixture
def flamp_api_json() -> dict:
    return load_json_fixture("flamp_api_response.json")


@pytest.fixture
def flamp_page_html() -> str:
    return load_fixture("flamp_page.html")


@pytest.fixture
def vk_groups_json() -> dict:
    return load_json_fixture("vk_groups_getbyid.json")


@pytest.fixture
def vk_wall_json() -> dict:
    return load_json_fixture("vk_wall_get.json")


@pytest.fixture
def otzovik_html() -> str:
    return load_fixture("otzovik_page.html")
