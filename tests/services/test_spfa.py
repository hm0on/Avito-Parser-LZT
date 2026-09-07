from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.spfa import SpfaClient, SpfaConfigError, SpfaLookupError, extract_avito_ad_id


class DummyResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_spfa_client_requires_credentials(monkeypatch):
    monkeypatch.setattr(
        "src.services.spfa.settings",
        SimpleNamespace(
            spfa_api_key="",
            spfa_phone_endpoint="https://spfa.ru/api/phone/",
            spfa_timeout_seconds=30.0,
        ),
    )

    with pytest.raises(SpfaConfigError):
        await SpfaClient().lookup_phone_by_ad_id("7385509771")


@pytest.mark.asyncio
async def test_spfa_client_extracts_phone_from_top_level_payload(monkeypatch):
    captured = {}

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["data"] = json
            return DummyResponse(200, {"phone": "+79991234567"})

    monkeypatch.setattr(
        "src.services.spfa.settings",
        SimpleNamespace(
            spfa_api_key="demo_key",
            spfa_phone_endpoint="https://spfa.ru/api/phone/",
            spfa_timeout_seconds=25.0,
        ),
    )
    monkeypatch.setattr("src.services.spfa.httpx.AsyncClient", DummyAsyncClient)

    result = await SpfaClient().lookup_phone_by_ad_id("7385509771")
    assert result.phone == "+79991234567"
    assert result.provider_status == 200
    assert result.ad_id == "7385509771"
    assert captured["url"] == "https://spfa.ru/api/phone/"
    assert captured["data"]["api_key"] == "demo_key"
    assert captured["data"]["ads"] == ["7385509771"]


@pytest.mark.asyncio
async def test_spfa_client_extracts_phone_from_nested_payload(monkeypatch):
    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            return DummyResponse(200, {"data": {"number": "+79990001122"}})

    monkeypatch.setattr(
        "src.services.spfa.settings",
        SimpleNamespace(
            spfa_api_key="demo_key",
            spfa_phone_endpoint="https://spfa.ru/api/phone/",
            spfa_timeout_seconds=25.0,
        ),
    )
    monkeypatch.setattr("src.services.spfa.httpx.AsyncClient", DummyAsyncClient)

    result = await SpfaClient().lookup_phone_by_ad_id("7385509771")
    assert result.phone == "+79990001122"


@pytest.mark.asyncio
async def test_spfa_client_extracts_phone_from_results_payload(monkeypatch):
    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            return DummyResponse(200, {"success": True, "results": [{"ad_id": "7385509771", "phone": "+79995554433"}]})

    monkeypatch.setattr(
        "src.services.spfa.settings",
        SimpleNamespace(
            spfa_api_key="demo_key",
            spfa_phone_endpoint="https://spfa.ru/api/phone/",
            spfa_timeout_seconds=25.0,
        ),
    )
    monkeypatch.setattr("src.services.spfa.httpx.AsyncClient", DummyAsyncClient)

    result = await SpfaClient().lookup_phone_by_ad_id("7385509771")
    assert result.phone == "+79995554433"


@pytest.mark.asyncio
async def test_spfa_client_raises_on_non_json(monkeypatch):
    class DummyResponse:
        status_code = 200

        def json(self):
            raise ValueError("bad json")

    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            return DummyResponse()

    monkeypatch.setattr(
        "src.services.spfa.settings",
        SimpleNamespace(
            spfa_api_key="demo_key",
            spfa_phone_endpoint="https://spfa.ru/api/phone/",
            spfa_timeout_seconds=25.0,
        ),
    )
    monkeypatch.setattr("src.services.spfa.httpx.AsyncClient", DummyAsyncClient)

    with pytest.raises(SpfaLookupError):
        await SpfaClient().lookup_phone_by_ad_id("7385509771")


@pytest.mark.asyncio
async def test_spfa_client_raises_when_phone_absent(monkeypatch):
    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, json, headers):
            return DummyResponse(200, {"success": False, "message": "not found"})

    monkeypatch.setattr(
        "src.services.spfa.settings",
        SimpleNamespace(
            spfa_api_key="demo_key",
            spfa_phone_endpoint="https://spfa.ru/api/phone/",
            spfa_timeout_seconds=25.0,
        ),
    )
    monkeypatch.setattr("src.services.spfa.httpx.AsyncClient", DummyAsyncClient)

    with pytest.raises(SpfaLookupError) as exc:
        await SpfaClient().lookup_phone_by_ad_id("7385509771")

    assert "not found" in str(exc.value)


def test_extract_avito_ad_id_from_listing_url():
    url = "https://www.avito.ru/omsk/predlozheniya_uslug/santehnik_uslugi_santehnika_7888000008"
    assert extract_avito_ad_id(url) == "7888000008"


def test_extract_avito_ad_id_from_brand_url_iid():
    url = "https://www.avito.ru/brands/391951ef61c9fe729a87110f0ef220b8?src=search_seller_info&iid=3824911571"
    assert extract_avito_ad_id(url) == "3824911571"
