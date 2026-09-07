"""Avito proxy pool precheck — fast httpx-based validation before collection."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx
import structlog

from src.config import settings

log = structlog.get_logger(__name__)

_BLOCK_MARKERS_LOWER = [
    "доступ ограничен",
    "firewall-container",
    "captcha",
    "blocked",
    "access denied",
    "429",
]

_TEST_URL_CONNECTIVITY = "https://api.ipify.org?format=text"
_TEST_URL_AVITO = "https://www.avito.ru/omsk"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


class ProxyPoolExhaustedError(RuntimeError):
    """Raised when not enough usable proxies survive precheck."""


@dataclass
class ProxyCheckResult:
    proxy_url: str
    connectivity_ok: bool = False
    avito_ok: bool = False
    avito_status: int | None = None
    latency_ms: float = 0.0
    error: str | None = None


@dataclass
class PrecheckSummary:
    total_tested: int = 0
    connectivity_ok: int = 0
    avito_ok: int = 0
    ranked_proxies: list[str] = field(default_factory=list)
    results: list[ProxyCheckResult] = field(default_factory=list)


async def check_single_proxy(
    proxy_url: str,
    timeout_s: float = 10.0,
    test_avito: bool = True,
) -> ProxyCheckResult:
    """Test a single proxy: connectivity via ipify, then Avito reachability."""
    result = ProxyCheckResult(proxy_url=proxy_url)
    start = time.monotonic()

    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=True,
        ) as client:
            # Phase 1: connectivity
            resp = await client.get(_TEST_URL_CONNECTIVITY)
            if resp.status_code == 200:
                result.connectivity_ok = True
            else:
                result.error = f"ipify status {resp.status_code}"
                result.latency_ms = (time.monotonic() - start) * 1000
                return result

            if not test_avito:
                result.latency_ms = (time.monotonic() - start) * 1000
                return result

            # Phase 2: Avito
            resp = await client.get(_TEST_URL_AVITO, headers=_HEADERS)
            result.avito_status = resp.status_code

            if resp.status_code in (429, 403):
                result.error = f"avito {resp.status_code}"
            else:
                body_lower = resp.text[:5000].lower()
                blocked = any(m in body_lower for m in _BLOCK_MARKERS_LOWER)
                if blocked:
                    result.error = "avito block marker in body"
                else:
                    result.avito_ok = True

    except Exception as exc:
        result.error = f"{type(exc).__name__}: {str(exc)[:200]}"

    result.latency_ms = (time.monotonic() - start) * 1000
    return result


async def run_precheck(
    proxy_urls: list[str],
    concurrency: int = 20,
    timeout_s: float = 10.0,
    sample_size: int = 0,
) -> PrecheckSummary:
    """Test proxy pool and return ranked summary."""
    if not proxy_urls:
        return PrecheckSummary()

    targets = proxy_urls
    if sample_size > 0 and sample_size < len(proxy_urls):
        # Random sample avoids bias when file is grouped by provider/port ranges.
        targets = random.sample(proxy_urls, sample_size)

    sem = asyncio.Semaphore(concurrency)

    async def _bounded(url: str) -> ProxyCheckResult:
        async with sem:
            return await check_single_proxy(url, timeout_s=timeout_s)

    results = await asyncio.gather(*[_bounded(u) for u in targets])

    summary = PrecheckSummary(
        total_tested=len(results),
        results=list(results),
    )

    tier1: list[ProxyCheckResult] = []
    tier2: list[ProxyCheckResult] = []

    for r in results:
        if r.connectivity_ok:
            summary.connectivity_ok += 1
        if r.avito_ok:
            summary.avito_ok += 1
            tier1.append(r)
        elif r.connectivity_ok:
            tier2.append(r)

    tier1.sort(key=lambda r: r.latency_ms)
    tier2.sort(key=lambda r: r.latency_ms)

    summary.ranked_proxies = [r.proxy_url for r in tier1] + [r.proxy_url for r in tier2]

    log.info(
        "avito.precheck.done",
        total=summary.total_tested,
        connectivity_ok=summary.connectivity_ok,
        avito_ok=summary.avito_ok,
        ranked_count=len(summary.ranked_proxies),
    )

    return summary
