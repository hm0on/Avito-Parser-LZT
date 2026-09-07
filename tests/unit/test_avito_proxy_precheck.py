"""Unit tests for avito_proxy_precheck module."""

import asyncio

import pytest

from src.collectors.avito_proxy_precheck import (
    PrecheckSummary,
    ProxyCheckResult,
    ProxyPoolExhaustedError,
    check_single_proxy,
    run_precheck,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, *, get_results=None, raise_on_get=None):
        self._get_results = get_results or {}
        self._raise_on_get = raise_on_get
        self._call_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        self._call_count += 1
        if self._raise_on_get:
            raise self._raise_on_get
        if url in self._get_results:
            return self._get_results[url]
        return _FakeResponse(200, "ok")


@pytest.mark.asyncio
async def test_check_single_proxy_connectivity_ok(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(
        "src.collectors.avito_proxy_precheck.httpx.AsyncClient",
        lambda **kw: client,
    )
    result = await check_single_proxy("socks5://u:p@1.2.3.4:1080", test_avito=False)
    assert result.connectivity_ok is True
    assert result.error is None


@pytest.mark.asyncio
async def test_check_single_proxy_connectivity_fail(monkeypatch):
    client = _FakeClient(raise_on_get=TimeoutError("timeout"))
    monkeypatch.setattr(
        "src.collectors.avito_proxy_precheck.httpx.AsyncClient",
        lambda **kw: client,
    )
    result = await check_single_proxy("socks5://u:p@1.2.3.4:1080")
    assert result.connectivity_ok is False
    assert result.avito_ok is False
    assert result.error is not None


@pytest.mark.asyncio
async def test_check_single_proxy_avito_429(monkeypatch):
    import src.collectors.avito_proxy_precheck as mod

    client = _FakeClient(get_results={
        mod._TEST_URL_CONNECTIVITY: _FakeResponse(200, "1.2.3.4"),
        mod._TEST_URL_AVITO: _FakeResponse(429, "Too Many Requests"),
    })
    monkeypatch.setattr(
        "src.collectors.avito_proxy_precheck.httpx.AsyncClient",
        lambda **kw: client,
    )
    result = await check_single_proxy("socks5://u:p@1.2.3.4:1080")
    assert result.connectivity_ok is True
    assert result.avito_ok is False
    assert result.avito_status == 429


@pytest.mark.asyncio
async def test_check_single_proxy_avito_block_body(monkeypatch):
    import src.collectors.avito_proxy_precheck as mod

    client = _FakeClient(get_results={
        mod._TEST_URL_CONNECTIVITY: _FakeResponse(200, "1.2.3.4"),
        mod._TEST_URL_AVITO: _FakeResponse(200, "<html>captcha required</html>"),
    })
    monkeypatch.setattr(
        "src.collectors.avito_proxy_precheck.httpx.AsyncClient",
        lambda **kw: client,
    )
    result = await check_single_proxy("socks5://u:p@1.2.3.4:1080")
    assert result.connectivity_ok is True
    assert result.avito_ok is False
    assert "block marker" in (result.error or "")


@pytest.mark.asyncio
async def test_run_precheck_ranks_avito_ok_first(monkeypatch):
    results_by_proxy = {}

    async def fake_check(proxy_url, timeout_s=10.0, test_avito=True):
        return results_by_proxy[proxy_url]

    monkeypatch.setattr(
        "src.collectors.avito_proxy_precheck.check_single_proxy",
        fake_check,
    )

    results_by_proxy["proxy1"] = ProxyCheckResult(
        proxy_url="proxy1", connectivity_ok=True, avito_ok=False, latency_ms=50,
    )
    results_by_proxy["proxy2"] = ProxyCheckResult(
        proxy_url="proxy2", connectivity_ok=True, avito_ok=True, latency_ms=100,
    )

    summary = await run_precheck(["proxy1", "proxy2"])
    assert summary.ranked_proxies[0] == "proxy2"  # avito_ok first


@pytest.mark.asyncio
async def test_run_precheck_sorts_by_latency(monkeypatch):
    results_by_proxy = {}

    async def fake_check(proxy_url, timeout_s=10.0, test_avito=True):
        return results_by_proxy[proxy_url]

    monkeypatch.setattr(
        "src.collectors.avito_proxy_precheck.check_single_proxy",
        fake_check,
    )

    results_by_proxy["slow"] = ProxyCheckResult(
        proxy_url="slow", connectivity_ok=True, avito_ok=True, latency_ms=500,
    )
    results_by_proxy["fast"] = ProxyCheckResult(
        proxy_url="fast", connectivity_ok=True, avito_ok=True, latency_ms=50,
    )

    summary = await run_precheck(["slow", "fast"])
    assert summary.ranked_proxies[0] == "fast"
    assert summary.ranked_proxies[1] == "slow"


@pytest.mark.asyncio
async def test_run_precheck_sample_size(monkeypatch):
    checked: list[str] = []

    async def fake_check(proxy_url, timeout_s=10.0, test_avito=True):
        checked.append(proxy_url)
        return ProxyCheckResult(proxy_url=proxy_url, connectivity_ok=True, avito_ok=True, latency_ms=100)

    monkeypatch.setattr(
        "src.collectors.avito_proxy_precheck.check_single_proxy",
        fake_check,
    )
    monkeypatch.setattr(
        "src.collectors.avito_proxy_precheck.random.sample",
        lambda seq, k: ["p3", "p1"],
    )

    await run_precheck(["p1", "p2", "p3", "p4"], sample_size=2)
    assert len(checked) == 2
    assert checked == ["p3", "p1"]


@pytest.mark.asyncio
async def test_run_precheck_empty_list():
    summary = await run_precheck([])
    assert summary.total_tested == 0
    assert summary.ranked_proxies == []


def test_proxy_pool_exhausted_is_runtime_error():
    err = ProxyPoolExhaustedError("no proxies")
    assert isinstance(err, RuntimeError)
