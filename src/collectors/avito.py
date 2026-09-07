"""Avito collector using Playwright browser + proxy rotation.

Flow:
1) Parse listing pages and collect company cards.
2) Run a second pass for seller reviews only for companies where reviews_count > 0.

No external cookie provider is used.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlencode, urljoin, urlparse

import structlog
from bs4 import BeautifulSoup

from src.collectors.base import AbstractCollector, RawCompany, RawReview, parse_float, parse_int
from src.config import settings
from src.proxy import avito_proxy_manager, require_proxy_pool

log = structlog.get_logger(__name__)

AVITO_BASE = "https://www.avito.ru"
AVITO_SEARCH_TEMPLATE = f"{AVITO_BASE}/{{area}}/predlozheniya_uslug"
_DEFAULT_AVITO_SEARCH = AVITO_SEARCH_TEMPLATE.format(area="omsk")
# Backward compatibility for tests/overrides that monkeypatch AVITO_SEARCH.
AVITO_SEARCH = _DEFAULT_AVITO_SEARCH

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Referer": "https://www.google.ru/search?q=avito+%D0%BE%D0%BC%D1%81%D0%BA",
}

_BLOCK_MARKERS = [
    "доступ ограничен",
    "firewall-container",
    "captcha",
    "access denied",
    "blocked",
]

_PROXY_ERROR_MARKERS = (
    "err_tunnel_connection_failed",
    "err_proxy_connection_failed",
    "err_socks_connection_failed",
    "err_connection_reset",
    "proxyconnect",
    "proxy connection",
    "ns_error_unknown_host",
    "econnrefused",
    "connection reset",
    "browser does not support socks5 proxy authentication",
    "socks5 proxy authentication",
    "timed out",
    "timeout",
    "winerror 10054",
    "winerror 10060",
    "name not resolved",
    "temporary failure in name resolution",
    "target page, context or browser has been closed",
    "target page/context/browser has been closed",
    "connection closed",
    "pipe is being closed",
)

_GOTO_TIMEOUT_CAP_MS = 90_000
_NETWORKIDLE_SOFT_TIMEOUT_MS = 10_000
_CONTENT_FALLBACK_TIMEOUT_S = 5.0

_REVIEW_LOAD_MORE_SELECTORS = (
    'button:has-text("Показать ещё")',
    'button:has-text("Показать еще")',
    '[role="button"]:has-text("Показать ещё")',
    '[role="button"]:has-text("Показать еще")',
)


class _AvitoBlockedError(RuntimeError):
    """Raised when Avito anti-bot / rate-limit response is detected."""


class _Socks5AuthBridge:
    """Local SOCKS5 server without auth that forwards through upstream SOCKS5 with auth."""

    def __init__(self, *, host: str, port: int, username: str, password: str) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._server: asyncio.base_events.Server | None = None
        self._listen_host = "127.0.0.1"
        self._listen_port = 0
        self._client_tasks: set[asyncio.Task[Any]] = set()

    @property
    def listen_port(self) -> int:
        return self._listen_port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self._listen_host, 0)
        sockets = self._server.sockets or []
        if not sockets:
            raise RuntimeError("Failed to start local SOCKS5 bridge")
        self._listen_port = sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        pending = [task for task in self._client_tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._client_tasks.clear()

    async def _on_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            await self._handle_client(client_reader, client_writer)
        finally:
            if task is not None:
                self._client_tasks.discard(task)

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        upstream_reader: asyncio.StreamReader | None = None
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            req = await self._read_client_request(client_reader, client_writer)
            if req is None:
                return
            atyp, addr_bytes, port_bytes = req

            upstream_reader, upstream_writer = await asyncio.open_connection(self._host, self._port)
            await self._auth_upstream(upstream_reader, upstream_writer)
            rep = await self._upstream_connect(
                upstream_reader,
                upstream_writer,
                atyp=atyp,
                addr_bytes=addr_bytes,
                port_bytes=port_bytes,
            )
            if rep != 0:
                await self._write_client_reply(client_writer, rep=rep)
                return

            await self._write_client_reply(client_writer, rep=0)
            await self._relay_bidirectional(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
            )
        except Exception:
            try:
                await self._write_client_reply(client_writer, rep=1)
            except Exception:
                pass
        finally:
            try:
                if upstream_writer:
                    upstream_writer.close()
                    await upstream_writer.wait_closed()
            except Exception:
                pass
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass

    async def _read_client_request(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> tuple[int, bytes, bytes] | None:
        head = await client_reader.readexactly(2)
        ver = head[0]
        nmethods = head[1]
        if ver != 5:
            return None
        methods = await client_reader.readexactly(nmethods)
        if 0x00 not in methods:
            client_writer.write(b"\x05\xff")
            await client_writer.drain()
            return None

        client_writer.write(b"\x05\x00")
        await client_writer.drain()

        req_head = await client_reader.readexactly(4)
        ver2, cmd, _rsv, atyp = req_head
        if ver2 != 5 or cmd != 1:
            await self._write_client_reply(client_writer, rep=7)
            return None

        addr_bytes = await self._read_address_bytes(client_reader, atyp)
        port_bytes = await client_reader.readexactly(2)
        return atyp, addr_bytes, port_bytes

    async def _auth_upstream(
        self,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        # Tell upstream SOCKS proxy we support no-auth and user/pass auth.
        upstream_writer.write(b"\x05\x02\x00\x02")
        await upstream_writer.drain()
        resp = await upstream_reader.readexactly(2)
        if resp[0] != 5:
            raise RuntimeError("Invalid SOCKS5 version from upstream proxy")
        method = resp[1]
        if method == 0x02:
            user_b = self._username.encode("utf-8")
            pass_b = self._password.encode("utf-8")
            auth_req = bytes([0x01, len(user_b)]) + user_b + bytes([len(pass_b)]) + pass_b
            upstream_writer.write(auth_req)
            await upstream_writer.drain()
            auth_resp = await upstream_reader.readexactly(2)
            if auth_resp[1] != 0x00:
                raise RuntimeError("SOCKS5 upstream auth failed")
            return
        if method == 0x00:
            return
        raise RuntimeError("SOCKS5 upstream rejected auth methods")

    async def _upstream_connect(
        self,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        *,
        atyp: int,
        addr_bytes: bytes,
        port_bytes: bytes,
    ) -> int:
        upstream_writer.write(b"\x05\x01\x00" + bytes([atyp]) + addr_bytes + port_bytes)
        await upstream_writer.drain()
        resp_head = await upstream_reader.readexactly(4)
        _ver, rep, _rsv, resp_atyp = resp_head
        _ = await self._read_address_bytes(upstream_reader, resp_atyp)
        _ = await upstream_reader.readexactly(2)  # bind port
        return rep

    async def _write_client_reply(self, client_writer: asyncio.StreamWriter, *, rep: int) -> None:
        # Reply with IPv4 0.0.0.0:0 as bind address.
        client_writer.write(bytes([0x05, rep, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]))
        await client_writer.drain()

    async def _read_address_bytes(self, reader: asyncio.StreamReader, atyp: int) -> bytes:
        if atyp == 0x01:  # IPv4
            return await reader.readexactly(4)
        if atyp == 0x03:  # domain
            ln = await reader.readexactly(1)
            return ln + await reader.readexactly(ln[0])
        if atyp == 0x04:  # IPv6
            return await reader.readexactly(16)
        raise RuntimeError(f"Unsupported SOCKS ATYP={atyp}")

    async def _relay_bidirectional(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
    ) -> None:
        async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()

        t1 = asyncio.create_task(_pipe(c_reader, u_writer))
        t2 = asyncio.create_task(_pipe(u_reader, c_writer))
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*(done | pending), return_exceptions=True)


class _PlaywrightBrowserClient:
    """Small async client wrapper around Playwright browser session."""

    def __init__(
        self,
        *,
        proxy: dict[str, str] | None,
        headless: bool,
        timeout_ms: int,
        browser_name: str,
    ) -> None:
        self._proxy = proxy
        self._proxy_hint = proxy.get("server", "direct") if proxy else "direct"
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._browser_name = (browser_name or "chromium").strip().lower()
        self._pw_cm: Any | None = None
        self._pw: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._proxy_bridge: _Socks5AuthBridge | None = None

    async def __aenter__(self) -> "_PlaywrightBrowserClient":
        from playwright.async_api import async_playwright

        self._pw_cm = async_playwright()
        self._pw = await self._pw_cm.__aenter__()
        browser_type = self._resolve_browser_type(self._pw)
        launch_proxy = await self._prepare_launch_proxy(self._proxy)
        self._browser = await browser_type.launch(
            headless=self._headless,
            proxy=launch_proxy,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(locale="ru-RU")
        await self._context.set_extra_http_headers(_HEADERS)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(self._timeout_ms)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._proxy_bridge:
            await self._proxy_bridge.close()
            self._proxy_bridge = None
        if self._pw_cm:
            await self._pw_cm.__aexit__(exc_type, exc, tb)

    def _resolve_browser_type(self, pw):
        if self._browser_name == "firefox":
            return pw.firefox
        if self._browser_name == "webkit":
            return pw.webkit
        return pw.chromium

    async def _prepare_launch_proxy(self, proxy: dict[str, str] | None) -> dict[str, str] | None:
        if not proxy:
            return None

        server = proxy.get("server", "")
        parsed = urlparse(server)
        if not parsed.scheme.startswith("socks"):
            return proxy

        username = proxy.get("username") or (unquote(parsed.username) if parsed.username else "")
        password = proxy.get("password") or (unquote(parsed.password) if parsed.password else "")
        if parsed.hostname and parsed.port and username and password:
            self._proxy_bridge = _Socks5AuthBridge(
                host=parsed.hostname,
                port=parsed.port,
                username=username,
                password=password,
            )
            await self._proxy_bridge.start()
            return {"server": f"socks5://127.0.0.1:{self._proxy_bridge.listen_port}"}

        if parsed.hostname and parsed.port:
            return {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}

        return proxy

    async def get_html(self, url: str, *, referer: str | None = None) -> tuple[int | None, str]:
        if not self._page:
            raise RuntimeError("Playwright page is not initialized")

        response = None
        goto_timeout = min(self._timeout_ms, _GOTO_TIMEOUT_CAP_MS)
        try:
            response = await self._page.goto(
                url,
                wait_until="domcontentloaded",
                referer=referer,
                timeout=goto_timeout,
            )
        except Exception as exc:
            html = await self._safe_page_content()
            if html and _looks_like_listing_html(html):
                log.warning(
                    "avito.fetch.partial_html_recovered",
                    url=url,
                    proxy=self._proxy_hint,
                    html_size=len(html),
                    error_short=str(exc)[:200],
                )
                return None, html
            raise
        status = response.status if response else None

        try:
            await self._page.wait_for_load_state(
                "networkidle",
                timeout=min(self._timeout_ms, _NETWORKIDLE_SOFT_TIMEOUT_MS),
            )
        except Exception:
            # Some pages never become network-idle; keep parsed HTML anyway.
            pass

        html = await self._safe_page_content()
        if status in (403, 429) or _is_blocked(html):
            raise _AvitoBlockedError(f"Blocked response for {url} (status={status})")
        return status, html

    async def get_profile_reviews_html(
        self,
        profile_url: str,
        *,
        referer: str | None = None,
        max_reviews: int,
    ) -> tuple[str, int]:
        """Open seller profile and click load-more while new review blocks appear."""
        if not self._page:
            raise RuntimeError("Playwright page is not initialized")

        response = None
        goto_timeout = min(self._timeout_ms, _GOTO_TIMEOUT_CAP_MS)
        try:
            response = await self._page.goto(
                profile_url,
                wait_until="domcontentloaded",
                referer=referer,
                timeout=goto_timeout,
            )
        except Exception as exc:
            html = await self._safe_page_content()
            if html and _looks_like_profile_html(html):
                log.warning(
                    "avito.reviews.partial_html_recovered",
                    url=profile_url,
                    proxy=self._proxy_hint,
                    html_size=len(html),
                    error_short=str(exc)[:200],
                )
                return html, 0
            raise
        status = response.status if response else None
        html = await self._safe_page_content()
        if status in (403, 429) or _is_blocked(html):
            raise _AvitoBlockedError(f"Blocked response for {profile_url} (status={status})")

        max_clicks = max(1, min(int(max_reviews), 120))
        load_more_clicks = 0
        prev_count = await self._review_blocks_count()

        while prev_count < max_reviews and load_more_clicks < max_clicks:
            button = await self._find_load_more_button()
            if not button:
                break
            try:
                await button.click(timeout=min(self._timeout_ms, 6_000))
            except Exception:
                break
            load_more_clicks += 1
            curr_count = await self._wait_for_review_count_growth(prev_count)
            if curr_count <= prev_count:
                break
            prev_count = curr_count

        html = await self._safe_page_content()
        return html, load_more_clicks

    async def _safe_page_content(self) -> str:
        if not self._page:
            return ""
        try:
            return await asyncio.wait_for(
                self._page.content(),
                timeout=_CONTENT_FALLBACK_TIMEOUT_S,
            )
        except Exception:
            return ""

    async def _review_blocks_count(self) -> int:
        if not self._page:
            return 0
        return await self._page.locator("[data-marker^='review(']").count()

    async def _find_load_more_button(self):
        if not self._page:
            return None
        for selector in _REVIEW_LOAD_MORE_SELECTORS:
            locator = self._page.locator(selector)
            if await locator.count() == 0:
                continue
            btn = locator.first
            try:
                if await btn.is_visible():
                    return btn
            except Exception:
                continue
        return None

    async def _wait_for_review_count_growth(self, previous_count: int) -> int:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.35)
            count = await self._review_blocks_count()
            if count > previous_count:
                return count
        return previous_count

    async def get_json(
        self,
        url: str,
        *,
        referer: str | None = None,
    ) -> tuple[int, dict | None, str, str]:
        if not self._context:
            raise RuntimeError("Playwright context is not initialized")

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if referer:
            headers["Referer"] = referer

        response = await self._context.request.get(
            url,
            headers=headers,
            timeout=self._timeout_ms,
        )
        status = response.status
        content_type = response.headers.get("content-type", "")
        text = await response.text()

        if status in (403, 429):
            raise _AvitoBlockedError(f"Blocked API response for {url} (status={status})")

        data: dict | None = None
        if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
            try:
                parsed = await response.json()
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                try:
                    parsed2 = json.loads(text)
                    if isinstance(parsed2, dict):
                        data = parsed2
                except Exception:
                    data = None

        return status, data, text, content_type


_PROXY_STATS_WINDOW = 20  # sliding window: only last N events matter


@dataclass
class _ProxyStats:
    """Tracks recent proxy outcomes in a sliding window.

    Proxies are rotating (exit IP changes on reconnect), so old failures
    must NOT permanently penalize a proxy.  Only the last _PROXY_STATS_WINDOW
    events are considered; older history is discarded automatically.
    """

    _events: list[str] = field(default_factory=list)  # "s", "b", "e"
    last_failure_ts: float = 0.0

    def _push(self, kind: str) -> None:
        self._events.append(kind)
        if len(self._events) > _PROXY_STATS_WINDOW:
            self._events = self._events[-_PROXY_STATS_WINDOW:]

    def record_success(self) -> None:
        self._push("s")

    def record_blocked(self) -> None:
        self._push("b")

    def record_error(self) -> None:
        self._push("e")

    @property
    def success_count(self) -> int:
        return self._events.count("s")

    @property
    def blocked_count(self) -> int:
        return self._events.count("b")

    @property
    def error_count(self) -> int:
        return self._events.count("e")

    def score(self) -> float:
        total = len(self._events)
        if total == 0:
            return 0.0
        success_rate = self.success_count / total
        penalty = (self.blocked_count * 2 + self.error_count) / total
        return (success_rate * 100) - (penalty * 50)


class AvitoCollector(AbstractCollector):
    source_name = "avito"

    def __init__(self) -> None:
        self._headless = bool(settings.avito_browser_headless)
        self._browser_name = settings.avito_browser_name
        self._avito_proxy_file = (settings.avito_proxy_file or "").strip()
        self._proxy_scheme = (settings.avito_proxy_scheme or "socks5").strip().lower()
        self._proxy_fallback_scheme = (
            (settings.avito_proxy_fallback_scheme or "").strip().lower()
        )
        self._proxy_max_rotations = max(1, int(settings.avito_proxy_max_rotations))
        self._proxy_fail_cooldown_s = max(0.0, float(settings.avito_proxy_fail_cooldown_seconds))
        self._request_min_interval_s = max(0.0, float(settings.avito_request_min_interval_seconds))
        self._timeout_ms = max(5_000, int(settings.avito_page_timeout_ms))
        self._fetch_retries = max(1, int(settings.avito_fetch_retries))
        self._max_pages = max(1, int(settings.avito_max_pages))
        self._review_pause_s = max(0.0, float(settings.avito_review_pause_seconds))
        self._review_workers = max(1, int(settings.avito_review_workers))
        self._max_reviews_per_company = max(1, int(settings.max_reviews_per_company))
        self._proxy_cooldowns: dict[str, float] = {}
        self._proxy_stats: dict[str, _ProxyStats] = {}
        self._retry_jitter_max_s = max(0.0, float(settings.avito_retry_jitter_max_seconds))
        self._last_request_ts = 0.0
        self._interval_lock = asyncio.Lock()
        self._avito_proxy_urls = self._load_avito_proxy_urls()
        self._avito_proxy_index = 0

    def _load_avito_proxy_urls(self) -> list[str]:
        if not self._avito_proxy_file:
            return []

        path = Path(self._avito_proxy_file)
        if not path.exists():
            log.warning("avito.proxy.file_not_found", path=str(path))
            return []

        lines = [
            line.strip().lstrip("\ufeff")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if lines:
            log.info("avito.proxy.file_loaded", path=str(path), count=len(lines))
        return lines

    async def precheck_proxies(self) -> "PrecheckSummary":
        """Run proxy precheck and reorder pool by quality. Returns summary."""
        from src.collectors.avito_proxy_precheck import (
            PrecheckSummary,
            ProxyPoolExhaustedError,
            run_precheck,
        )

        if not settings.avito_precheck_enabled:
            return PrecheckSummary()

        proxy_urls = list(self._avito_proxy_urls)
        if not proxy_urls:
            raw_proxies = avito_proxy_manager._proxies  # noqa: SLF001
            proxy_urls = list(raw_proxies) if raw_proxies else []

        if not proxy_urls:
            log.warning("avito.precheck.no_proxies")
            return PrecheckSummary()

        summary = await run_precheck(
            proxy_urls,
            concurrency=settings.avito_precheck_concurrency,
            timeout_s=settings.avito_precheck_timeout_seconds,
            sample_size=settings.avito_precheck_sample_size,
        )

        min_usable = max(1, settings.avito_precheck_min_usable)
        min_avito_ok = max(0, int(settings.avito_precheck_min_avito_ok))
        usable = len(summary.ranked_proxies)
        if usable < min_usable:
            raise ProxyPoolExhaustedError(
                f"Only {usable} usable proxies (need {min_usable}). "
                f"Tested {summary.total_tested}, connectivity_ok={summary.connectivity_ok}, "
                f"avito_ok={summary.avito_ok}"
            )
        if min_avito_ok > 0 and summary.avito_ok < min_avito_ok:
            raise ProxyPoolExhaustedError(
                f"Only {summary.avito_ok} Avito-usable proxies (need {min_avito_ok}). "
                f"Tested {summary.total_tested}, connectivity_ok={summary.connectivity_ok}, "
                f"ranked={usable}"
            )

        if summary.ranked_proxies:
            self._avito_proxy_urls = summary.ranked_proxies
            self._avito_proxy_index = 0
            self._proxy_stats.clear()
            log.info(
                "avito.precheck.reordered",
                ranked_count=len(summary.ranked_proxies),
                avito_ok=summary.avito_ok,
            )

        return summary

    def proxy_stats_summary(self) -> dict[str, Any]:
        """Return stats for each proxy for logging/debugging."""
        now = time.monotonic()
        out: dict[str, Any] = {}
        for proxy_id, stats in self._proxy_stats.items():
            cooldown_until = self._proxy_cooldowns.get(proxy_id, 0.0)
            out[proxy_id] = {
                "score": round(stats.score(), 1),
                "success": stats.success_count,
                "blocked": stats.blocked_count,
                "errors": stats.error_count,
                "cooldown_remaining_s": round(max(0.0, cooldown_until - now), 1),
            }
        return out

    def _proxy_count(self) -> int:
        return len(self._avito_proxy_urls) if self._avito_proxy_urls else avito_proxy_manager.count

    def _ensure_proxy_available(self) -> None:
        if self._avito_proxy_urls:
            return
        require_proxy_pool(avito_proxy_manager, purpose="avito collection")

    def _attempt_limit(self) -> int:
        proxy_count = self._proxy_count()
        if proxy_count <= 0:
            return self._fetch_retries
        return max(self._fetch_retries, self._proxy_max_rotations)

    def _proxy_id(self, proxy: dict[str, str] | None) -> str:
        return proxy.get("server", "direct") if proxy else "direct"

    async def _pick_proxy(self) -> dict[str, str] | None:
        self._ensure_proxy_available()
        proxy_count = self._proxy_count()
        if proxy_count <= 0:
            raise RuntimeError("Avito proxy pool is empty")

        now = time.monotonic()
        candidates: list[dict[str, str]] = []
        best_cooling_proxy: dict[str, str] | None = None
        best_cooling_wait: float | None = None
        scan_limit = min(proxy_count, max(1, self._proxy_max_rotations * 3))

        for _ in range(scan_limit):
            candidate = self._next_proxy()
            if not candidate:
                raise RuntimeError("Failed to acquire a valid Avito proxy from configured pool")

            cooldown_until = self._proxy_cooldowns.get(self._proxy_id(candidate), 0.0)
            if cooldown_until <= now:
                candidates.append(candidate)
                if len(candidates) >= 3:
                    break
            else:
                remaining = cooldown_until - now
                if best_cooling_wait is None or remaining < best_cooling_wait:
                    best_cooling_proxy = candidate
                    best_cooling_wait = remaining

        if candidates:
            candidates.sort(
                key=lambda p: self._proxy_stats.get(
                    self._proxy_id(p), _ProxyStats()
                ).score(),
                reverse=True,
            )
            return candidates[0]

        if best_cooling_proxy:
            wait_s = max(0.0, float(best_cooling_wait or 0.0))
            if wait_s > 0:
                sleep_s = min(60.0, wait_s)
                log.info(
                    "avito.proxy.wait_for_cooldown",
                    min_wait_s=round(wait_s, 1),
                    sleep_s=round(sleep_s, 1),
                )
                await asyncio.sleep(sleep_s)
                return await self._pick_proxy()
            return best_cooling_proxy
        raise RuntimeError("No usable Avito proxy available")

    def _next_proxy(self) -> dict[str, str] | None:
        if self._avito_proxy_urls:
            for _ in range(len(self._avito_proxy_urls)):
                raw = self._avito_proxy_urls[self._avito_proxy_index % len(self._avito_proxy_urls)]
                self._avito_proxy_index += 1
                parsed = self._proxy_from_url(raw, scheme=self._proxy_scheme)
                if parsed:
                    return parsed
            return None
        scheme = None if self._proxy_scheme in ("", "auto") else self._proxy_scheme
        return avito_proxy_manager.playwright_proxy(scheme=scheme)

    def _proxy_from_url(self, raw_url: str, *, scheme: str) -> dict[str, str] | None:
        raw = (raw_url or "").strip()
        if not raw:
            return None
        if "://" not in raw:
            default_scheme = (scheme or "socks5").strip().lower()
            raw = f"{default_scheme}://{raw}"

        parsed = urlparse(raw)
        if not parsed.hostname or not parsed.port:
            return None

        raw_scheme = (parsed.scheme or "").strip().lower()
        configured_scheme = (scheme or "").strip().lower()
        if configured_scheme in ("", "auto"):
            selected_scheme = raw_scheme or "http"
        elif raw_scheme and raw_scheme != configured_scheme:
            # Prefer explicit scheme from proxy line to avoid protocol mismatch.
            selected_scheme = raw_scheme
            log.warning(
                "avito.proxy.scheme_mismatch",
                configured_scheme=configured_scheme,
                raw_scheme=raw_scheme,
                effective_scheme=selected_scheme,
            )
        else:
            selected_scheme = configured_scheme or raw_scheme or "http"

        if selected_scheme.startswith("socks"):
            auth = ""
            if parsed.username and parsed.password:
                user = quote(unquote(parsed.username), safe="")
                pwd = quote(unquote(parsed.password), safe="")
                auth = f"{user}:{pwd}@"
            return {"server": f"{selected_scheme}://{auth}{parsed.hostname}:{parsed.port}"}

        proxy: dict[str, str] = {"server": f"{selected_scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy

    def _mark_proxy_failed(self, proxy: dict[str, str] | None, *, reason: str) -> None:
        if not proxy:
            return

        proxy_id = self._proxy_id(proxy)
        stats = self._proxy_stats.setdefault(proxy_id, _ProxyStats())
        if reason in ("blocked", "avito_429", "avito_403"):
            stats.record_blocked()
        else:
            stats.record_error()
        stats.last_failure_ts = time.monotonic()

        proxy_count = self._proxy_count()
        if proxy_count <= 1:
            return

        cooldown_s = 0.0 if reason == "proxy_error" else self._proxy_fail_cooldown_s
        if cooldown_s <= 0:
            self._proxy_cooldowns.pop(proxy_id, None)
            log.debug("avito.proxy.cooldown_skipped", proxy=proxy_id, reason=reason)
            return

        self._proxy_cooldowns[proxy_id] = time.monotonic() + cooldown_s
        log.info(
            "avito.proxy.cooldown_set",
            proxy=proxy_id,
            reason=reason,
            cooldown_s=cooldown_s,
            score=round(stats.score(), 1),
        )

    def _mark_proxy_success(self, proxy: dict[str, str] | None) -> None:
        if not proxy:
            return
        proxy_id = self._proxy_id(proxy)
        stats = self._proxy_stats.setdefault(proxy_id, _ProxyStats())
        stats.record_success()
        self._proxy_cooldowns.pop(proxy_id, None)

    async def _respect_min_interval(self) -> None:
        async with self._interval_lock:
            if self._request_min_interval_s <= 0:
                self._last_request_ts = time.monotonic()
                return

            now = time.monotonic()
            elapsed = now - self._last_request_ts
            if elapsed < self._request_min_interval_s:
                await asyncio.sleep(self._request_min_interval_s - elapsed)
            self._last_request_ts = time.monotonic()

    def _maybe_fallback_proxy_scheme(self, exc: Exception) -> None:
        if not _is_socks_proxy_error(exc):
            return
        if not self._proxy_scheme.startswith("socks5"):
            return
        if not self._proxy_fallback_scheme or self._proxy_fallback_scheme == self._proxy_scheme:
            return
        log.warning(
            "avito.proxy.scheme_fallback",
            from_scheme=self._proxy_scheme,
            to_scheme=self._proxy_fallback_scheme,
            reason=str(exc)[:200],
        )
        self._proxy_scheme = self._proxy_fallback_scheme

    def _retry_wait_s(self, *, attempt: int, proxy_error: bool) -> float:
        if proxy_error and self._proxy_count() > 1:
            base = min(60.0, max(1.0, self._request_min_interval_s))
        else:
            base = min(60.0, float((3 * attempt) if proxy_error else (2 * attempt)))
        if self._retry_jitter_max_s > 0:
            base += random.uniform(0, self._retry_jitter_max_s)
        return min(60.0, base)

    async def enrich_phones(self, companies: list[RawCompany]) -> None:
        # Kept for interface compatibility with pipeline runner.
        if any(c.source == "avito" for c in companies):
            log.info("avito.phones.skipped", reason="cookies_provider_removed")

    async def enrich_reviews(self, companies: list[RawCompany]) -> None:
        if not settings.enable_avito_review_parsing:
            log.info("avito.reviews.disabled")
            return

        candidates = [
            c
            for c in companies
            if self._should_fetch_reviews(c)
            and not c.reviews
        ]
        if not candidates:
            return

        workers = max(1, min(self._review_workers, len(candidates)))
        log.info("avito.reviews.start", count=len(candidates), workers=workers)

        async def _process_company(company: RawCompany) -> int:
            fetched = await self._fetch_seller_reviews_with_retries(company)
            if isinstance(fetched, tuple):
                reviews, meta = fetched
            else:
                reviews, meta = fetched, {}
            if reviews:
                company.reviews = reviews
                log.info(
                    "avito.reviews.ok",
                    name=company.name_raw,
                    source_id=company.source_id,
                    count=len(reviews),
                    profile_type=meta.get("profile_type"),
                    load_more_clicks=meta.get("load_more_clicks", 0),
                    reviews_parsed=meta.get("reviews_parsed", len(reviews)),
                    reviews_limited=meta.get("reviews_limited", False),
                )
            else:
                log.debug(
                    "avito.reviews.empty",
                    name=company.name_raw,
                    source_id=company.source_id,
                    profile_type=meta.get("profile_type"),
                    load_more_clicks=meta.get("load_more_clicks", 0),
                    reviews_parsed=meta.get("reviews_parsed", 0),
                    reviews_limited=meta.get("reviews_limited", False),
                )

            if self._review_pause_s > 0:
                await asyncio.sleep(self._review_pause_s)
            return len(reviews)

        total_reviews = 0
        if workers == 1:
            for company in candidates:
                total_reviews += await _process_company(company)
        else:
            sem = asyncio.Semaphore(workers)

            async def _run_with_sem(company: RawCompany) -> int:
                async with sem:
                    return await _process_company(company)

            results = await asyncio.gather(
                *[_run_with_sem(company) for company in candidates],
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    log.warning("avito.reviews.worker_error", error=str(result)[:200])
                    continue
                total_reviews += int(result)

        log.info("avito.reviews.done", companies=len(candidates), total_reviews=total_reviews)

    def _should_fetch_reviews(self, company: RawCompany) -> bool:
        if company.source != "avito":
            return False

        reviews_count = company.reviews_count
        if reviews_count is None or reviews_count <= 0:
            log.debug(
                "avito.reviews.skip_no_review_signal",
                source_id=company.source_id,
                source_link=company.source_link,
                reviews_count=reviews_count,
            )
            return False

        payload = company.raw_payload if isinstance(company.raw_payload, dict) else {}
        seller_hint = str(payload.get("seller_link") or "").strip()
        if seller_hint or company.source_link:
            return True

        log.debug(
            "avito.reviews.skip_missing_links",
            source_id=company.source_id,
            reviews_count=reviews_count,
        )
        return False

    async def _fetch_seller_reviews_with_retries(
        self,
        company: RawCompany,
    ) -> tuple[list[RawReview], dict[str, Any]]:
        self._ensure_proxy_available()
        max_attempts = self._attempt_limit()
        last_meta: dict[str, Any] = {}
        for attempt in range(1, max_attempts + 1):
            await self._respect_min_interval()
            proxy = await self._pick_proxy()
            proxy_id = self._proxy_id(proxy)
            try:
                async with _PlaywrightBrowserClient(
                    proxy=proxy,
                    headless=self._headless,
                    timeout_ms=self._timeout_ms,
                    browser_name=self._browser_name,
                ) as client:
                    result, meta = await self._fetch_seller_reviews(company, client)
                    self._mark_proxy_success(proxy)
                    return result, meta
            except _AvitoBlockedError as exc:
                self._mark_proxy_failed(proxy, reason="blocked")
                wait = min(60.0, max(3.0, self._request_min_interval_s))
                log.warning(
                    "avito.reviews.blocked",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    wait_s=wait,
                    proxy=proxy_id,
                    source_id=company.source_id,
                    error=str(exc),
                )
                if wait > 0:
                    await asyncio.sleep(wait)
                else:
                    log.info(
                        "avito.proxy.retry_immediate",
                        stage="reviews",
                        reason="blocked",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        proxy=proxy_id,
                        error_short=str(exc)[:200],
                    )
                    self._last_request_ts = 0.0
            except Exception as exc:
                last_meta = {"error": str(exc)[:200]}
                self._maybe_fallback_proxy_scheme(exc)
                proxy_error = _should_treat_as_proxy_error(exc)
                if proxy_error:
                    self._mark_proxy_failed(proxy, reason="proxy_error")
                wait = self._retry_wait_s(attempt=attempt, proxy_error=proxy_error)
                log.warning(
                    "avito.reviews.error",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    wait_s=wait,
                    proxy=proxy_id,
                    proxy_error=proxy_error,
                    source_id=company.source_id,
                    error=str(exc),
                )
                if wait > 0:
                    await asyncio.sleep(wait)
                else:
                    if proxy_error:
                        log.info(
                            "avito.proxy.retry_immediate",
                            stage="reviews",
                            reason="proxy_error",
                            attempt=attempt,
                            max_attempts=max_attempts,
                            proxy=proxy_id,
                            error_short=str(exc)[:200],
                        )
                    self._last_request_ts = 0.0

        return [], last_meta

    async def _fetch_seller_reviews(
        self,
        company: RawCompany,
        client: _PlaywrightBrowserClient,
    ) -> tuple[list[RawReview], dict[str, Any]]:
        source_link = company.source_link or ""
        payload = company.raw_payload if isinstance(company.raw_payload, dict) else {}
        seller_hint = str(payload.get("seller_link") or "").strip()
        profile_url = _extract_profile_url_from_source_link(seller_hint) if seller_hint else None
        if not profile_url:
            profile_url = _extract_profile_url_from_source_link(source_link)
        seller_path = ""
        ad_html = ""
        if profile_url:
            seller_path = urlparse(profile_url).path or profile_url
        else:
            ad_html = await self._fetch_html_via_client(client, source_link)
            if not ad_html:
                return [], {}

            if _looks_like_profile_html(ad_html):
                ad_reviews = _parse_reviews_from_html(
                    ad_html,
                    company.source_link,
                    max_reviews=self._max_reviews_per_company,
                )
                if ad_reviews:
                    return ad_reviews, {
                        "profile_type": "ad_page",
                        "load_more_clicks": 0,
                        "reviews_parsed": len(ad_reviews),
                        "reviews_limited": len(ad_reviews) >= self._max_reviews_per_company,
                    }

            rating_caption_path = _extract_rating_caption_path(ad_html)
            if rating_caption_path:
                seller_path = rating_caption_path
                profile_url = (
                    seller_path if seller_path.startswith("http") else urljoin(AVITO_BASE, seller_path)
                )
            else:
                seller_path = _extract_seller_path(ad_html) or ""
                if not seller_path:
                    log.debug("avito.reviews.no_seller", source_link=source_link)
                    return [], {}
                profile_url = (
                    seller_path if seller_path.startswith("http") else urljoin(AVITO_BASE, seller_path)
                )

        profile_type = _profile_type(profile_url)
        meta: dict[str, Any] = {
            "profile_type": profile_type,
            "load_more_clicks": 0,
            "reviews_parsed": 0,
            "reviews_limited": False,
        }
        referer = source_link if source_link and source_link != profile_url else None

        profile_html, load_more_clicks = await client.get_profile_reviews_html(
            profile_url,
            referer=referer,
            max_reviews=self._max_reviews_per_company,
        )
        meta["load_more_clicks"] = load_more_clicks

        dom_reviews = _parse_reviews_from_html(
            profile_html,
            company.source_link,
            max_reviews=self._max_reviews_per_company,
        )
        if dom_reviews:
            meta["reviews_parsed"] = len(dom_reviews)
            meta["reviews_limited"] = len(dom_reviews) >= self._max_reviews_per_company
            return dom_reviews, meta

        ratings_api = _extract_ratings_api_path(profile_html)
        if not ratings_api:
            user_hash = _extract_user_hash(seller_path)
            if not user_hash:
                return [], meta
            ratings_api = f"/web/6/user/{user_hash}/ratings"

        api_reviews = await self._fetch_ratings_api(
            client=client,
            api_path=ratings_api,
            referer=profile_url,
            source_link=company.source_link,
        )
        if api_reviews:
            api_reviews = api_reviews[: self._max_reviews_per_company]
            meta["reviews_parsed"] = len(api_reviews)
            meta["reviews_limited"] = len(api_reviews) >= self._max_reviews_per_company
            return api_reviews, meta
        return [], meta

    async def _fetch_ratings_api(
        self,
        *,
        client: _PlaywrightBrowserClient,
        api_path: str,
        referer: str,
        source_link: str,
    ) -> list[RawReview]:
        versions_to_try = [6, 5, 4]
        base_path = re.sub(r"/web/\d+/", "/web/{ver}/", api_path)

        for ver in versions_to_try:
            path = base_path.replace("{ver}", str(ver))
            url = f"{AVITO_BASE}{path}"
            status, data, text, content_type = await client.get_json(url, referer=referer)

            if status == 404:
                log.debug("avito.reviews.api_404", ver=ver)
                continue

            if status >= 400:
                log.debug("avito.reviews.api_http_error", status=status, ver=ver)
                continue

            if not data:
                log.debug(
                    "avito.reviews.api_not_json",
                    ver=ver,
                    content_type=content_type,
                    body=text[:120],
                )
                continue

            reviews = _parse_ratings_json(data, source_link)
            if reviews:
                log.info("avito.reviews.api_ok", ver=ver, count=len(reviews))
                return reviews

        return []

    async def collect(self, keyword: str, *, max_companies: int = 0) -> list[RawCompany]:
        self._ensure_proxy_available()
        urls = self._build_search_urls(keyword)
        all_companies: list[RawCompany] = []
        seen_ids: set[str] = set()
        seen_links: set[str] = set()

        for url in urls:
            if max_companies and len(all_companies) >= max_companies:
                break
            log.info("avito.collect.start", keyword=keyword, url=url)
            remaining = max(0, max_companies - len(all_companies)) if max_companies else 0
            companies = await self._collect_for_url(
                keyword=keyword,
                url=url,
                max_companies=remaining,
            )
            for company in companies:
                source_id = (company.source_id or "").strip()
                source_link = (company.source_link or "").strip()
                if source_id and source_id in seen_ids:
                    continue
                if source_link and source_link in seen_links:
                    continue
                if source_id:
                    seen_ids.add(source_id)
                if source_link:
                    seen_links.add(source_link)
                all_companies.append(company)
                if max_companies and len(all_companies) >= max_companies:
                    break

        log.info("avito.collect.done", keyword=keyword, count=len(all_companies), urls=len(urls))
        return all_companies

    async def _collect_for_url(
        self,
        *,
        keyword: str,
        url: str,
        max_companies: int = 0,
    ) -> list[RawCompany]:
        max_attempts = self._attempt_limit()
        for attempt in range(1, max_attempts + 1):
            await self._respect_min_interval()
            proxy = await self._pick_proxy()
            proxy_id = self._proxy_id(proxy)
            try:
                async with _PlaywrightBrowserClient(
                    proxy=proxy,
                    headless=self._headless,
                    timeout_ms=self._timeout_ms,
                    browser_name=self._browser_name,
                ) as client:
                    try:
                        companies = await self._collect_with_client(
                            keyword,
                            url,
                            client,
                            max_companies=max_companies,
                        )
                    except TypeError as exc:
                        # Backward compatibility for monkeypatched tests/helpers
                        # using legacy _collect_with_client(keyword, url, client).
                        if "max_companies" not in str(exc):
                            raise
                        companies = await self._collect_with_client(
                            keyword,
                            url,
                            client,
                        )
                    self._mark_proxy_success(proxy)
                    log.info(
                        "avito.collect.done",
                        keyword=keyword,
                        count=len(companies),
                        url=url,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        proxy=proxy_id,
                    )
                    return companies
            except _AvitoBlockedError as exc:
                self._mark_proxy_failed(proxy, reason="blocked")
                wait = min(60.0, max(3.0, self._request_min_interval_s))
                log.warning(
                    "avito.collect.blocked",
                    keyword=keyword,
                    url=url,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    wait_s=wait,
                    proxy=proxy_id,
                    error=str(exc),
                )
                if wait > 0:
                    await asyncio.sleep(wait)
                else:
                    log.info(
                        "avito.proxy.retry_immediate",
                        stage="collect",
                        reason="blocked",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        proxy=proxy_id,
                        error_short=str(exc)[:200],
                    )
                    self._last_request_ts = 0.0
            except Exception as exc:
                self._maybe_fallback_proxy_scheme(exc)
                proxy_error = _should_treat_as_proxy_error(exc)
                if proxy_error:
                    self._mark_proxy_failed(proxy, reason="proxy_error")
                wait = self._retry_wait_s(attempt=attempt, proxy_error=proxy_error)
                log.warning(
                    "avito.collect.error",
                    keyword=keyword,
                    url=url,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    wait_s=wait,
                    proxy=proxy_id,
                    proxy_error=proxy_error,
                    error=str(exc),
                )
                if wait > 0:
                    await asyncio.sleep(wait)
                else:
                    if proxy_error:
                        log.info(
                            "avito.proxy.retry_immediate",
                            stage="collect",
                            reason="proxy_error",
                            attempt=attempt,
                            max_attempts=max_attempts,
                            proxy=proxy_id,
                            error_short=str(exc)[:200],
                        )
                    self._last_request_ts = 0.0

        log.warning("avito.collect.gave_up", keyword=keyword, url=url)
        return []

    def _build_search_urls(self, keyword: str) -> list[str]:
        query = quote_plus(keyword)
        if AVITO_SEARCH != _DEFAULT_AVITO_SEARCH:
            return [f"{AVITO_SEARCH}?q={query}"]

        urls: list[str] = []
        for area in settings.avito_search_areas_list:
            area_clean = area.strip().strip("/")
            if not area_clean:
                continue
            urls.append(f"{AVITO_SEARCH_TEMPLATE.format(area=area_clean)}?q={query}")

        if not urls:
            urls.append(f"{AVITO_SEARCH_TEMPLATE.format(area='omsk')}?q={query}")
        return list(dict.fromkeys(urls))

    async def _collect_with_client(
        self,
        keyword: str,
        base_url: str,
        client: _PlaywrightBrowserClient,
        *,
        max_companies: int = 0,
    ) -> list[RawCompany]:
        companies: list[RawCompany] = []
        next_page_url: str | None = base_url
        visited_urls: set[str] = set()
        city_total_limit: int | None = None

        for page in range(1, self._max_pages + 1):
            if not next_page_url:
                break
            if next_page_url in visited_urls:
                log.warning(
                    "avito.collect.pagination_loop",
                    page=page,
                    keyword=keyword,
                    url=next_page_url,
                )
                break
            visited_urls.add(next_page_url)

            page_url = next_page_url
            html_text = await self._fetch_html_via_client(client, page_url)
            if not html_text:
                break

            page_companies_all = _parse_html(html_text, keyword)
            if not page_companies_all:
                log.info("avito.collect.empty_page", page=page, keyword=keyword)
                break

            local_ids = _extract_current_city_item_ids(html_text)
            if local_ids is not None:
                local_count = sum(
                    1 for company in page_companies_all if company.source_id and company.source_id in local_ids
                )
                if local_count < len(page_companies_all):
                    log.info(
                        "avito.collect.local_hint_on_page",
                        keyword=keyword,
                        page=page,
                        local_count=local_count,
                        page_count=len(page_companies_all),
                    )
            page_companies = page_companies_all

            if page == 1:
                city_total_limit = _extract_page_total_count(html_text)
                if city_total_limit:
                    log.info(
                        "avito.collect.city_total_detected",
                        keyword=keyword,
                        city_total_limit=city_total_limit,
                    )
            if max_companies:
                page_companies = page_companies[: max(0, max_companies - len(companies))]

            companies.extend(page_companies)
            log.info(
                "avito.collect.page_done",
                page=page,
                found=len(page_companies),
                total=len(companies),
                keyword=keyword,
            )
            if max_companies and len(companies) >= max_companies:
                log.info(
                    "avito.collect.limit_reached",
                    keyword=keyword,
                    page=page,
                    limit=max_companies,
                    total=len(companies),
                )
                companies = companies[:max_companies]
                break
            if city_total_limit and len(companies) >= city_total_limit:
                companies = companies[:city_total_limit]
                log.info(
                    "avito.collect.city_total_reached",
                    keyword=keyword,
                    page=page,
                    city_total_limit=city_total_limit,
                    total=len(companies),
                )
                break
            next_href = _extract_next_page_url(html_text)
            if next_href:
                next_page_url = urljoin(AVITO_BASE, next_href)
            elif city_total_limit and len(companies) < city_total_limit:
                remaining = city_total_limit - len(companies)
                if remaining <= 50:
                    fallback_next = _increment_page_url(page_url)
                    next_page_url = fallback_next if fallback_next not in visited_urls else None
                    if next_page_url:
                        log.info(
                            "avito.collect.next_page_fallback",
                            keyword=keyword,
                            page=page,
                            next_url=next_page_url,
                            total=len(companies),
                            city_total_limit=city_total_limit,
                        )
                else:
                    next_page_url = None
            else:
                next_page_url = None
            await asyncio.sleep(1.0)

        return companies

    async def _fetch_html_via_client(
        self,
        client: _PlaywrightBrowserClient,
        url: str,
        *,
        referer: str | None = None,
    ) -> str | None:
        status, html = await client.get_html(url, referer=referer)
        if status is not None and status >= 400:
            log.debug("avito.fetch.http_error", status=status, url=url)
            return None
        return html


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _parse_html(html_text: str, keyword: str) -> list[RawCompany]:
    """Parse listing cards from Avito HTML."""
    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.find_all(attrs={"data-item-id": True})

    companies: list[RawCompany] = []
    for card in cards:
        company = _parse_card(card, keyword)
        if company:
            companies.append(company)
    return companies


def _parse_card(card, keyword: str) -> RawCompany | None:
    item_id = card.get("data-item-id")

    title_el = card.find(attrs={"data-marker": "item-title"})
    name_raw = title_el.get_text(strip=True) if title_el else None
    if not name_raw:
        return None

    href = title_el.get("href", "") if title_el else ""
    clean_path = href.split("?")[0]
    source_link = f"{AVITO_BASE}{clean_path}" if clean_path.startswith("/") else None
    seller_path = _extract_card_seller_path(card)
    seller_link = urljoin(AVITO_BASE, seller_path) if seller_path else None

    loc_el = card.find(attrs={"data-marker": "item-location"})
    addresses: list[str] = []
    if loc_el:
        loc_text = loc_el.get_text(strip=True)
        if loc_text:
            addresses.append(loc_text)

    rating_el = card.find(attrs={"data-marker": "seller-rating/score"}) or card.find(
        attrs={"data-marker": "seller-rating"}
    )
    average_rating = parse_float(rating_el.get_text(strip=True)) if rating_el else None

    reviews_el = card.find(attrs={"data-marker": "seller-info/summary"})
    reviews_count = parse_int(reviews_el.get_text(strip=True)) if reviews_el else None
    raw_payload = {"keyword": keyword, "item_id": item_id}
    if seller_link:
        raw_payload["seller_link"] = seller_link

    return RawCompany(
        source="avito",
        source_id=item_id,
        source_link=source_link,
        name_raw=name_raw,
        addresses=addresses,
        average_rating=average_rating,
        reviews_count=reviews_count,
        raw_payload=raw_payload,
    )


def _extract_card_seller_path(card) -> str | None:
    """Extract seller profile path directly from listing card, preferring brand pages."""
    user_candidate: str | None = None
    for link in card.find_all("a", href=True):
        href = str(link.get("href") or "").strip()
        cleaned = _clean_seller_href(href)
        if not cleaned:
            continue
        low_cleaned = cleaned.lower()
        if "/brands/" in low_cleaned:
            return cleaned
        if "/user/" in low_cleaned and not user_candidate:
            user_candidate = cleaned
    return user_candidate


def _extract_next_page_url(html_text: str) -> str | None:
    """Extract next page href from Avito pagination nav."""
    soup = BeautifulSoup(html_text, "html.parser")
    next_link = soup.find("a", attrs={"data-marker": "pagination-button/nextPage"}, href=True)
    if not next_link:
        return None
    href = str(next_link.get("href", "")).strip()
    return href or None


def _increment_page_url(current_url: str) -> str | None:
    """Build next page URL by incrementing query param p (fallback when next link is absent)."""
    parsed = urlparse(current_url)
    if not parsed.scheme or not parsed.netloc:
        return None

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    updated: list[tuple[str, str]] = []
    had_page = False
    for key, value in pairs:
        if key == "p" and not had_page:
            had_page = True
            try:
                page_no = max(1, int(value))
            except (TypeError, ValueError):
                page_no = 1
            updated.append(("p", str(page_no + 1)))
            continue
        updated.append((key, value))

    if not had_page:
        updated.append(("p", "2"))

    return parsed._replace(query=urlencode(updated, doseq=True)).geturl()


def _extract_page_total_count(html_text: str) -> int | None:
    """Extract total ads count for current city/query from page title counter."""
    soup = BeautifulSoup(html_text, "html.parser")
    count_el = soup.find(attrs={"data-marker": "page-title/count"})
    if not count_el:
        return None
    return parse_int(count_el.get_text(" ", strip=True))


def _extract_current_city_item_ids(html_text: str) -> set[str] | None:
    """Extract item IDs that belong to the current city from embedded MFE state."""
    soup = BeautifulSoup(html_text, "html.parser")
    state_script = soup.find("script", attrs={"data-mfe-state": "true"})
    if not state_script:
        return None

    raw_state = state_script.get_text()
    if not raw_state:
        return None

    try:
        payload = json.loads(html_mod.unescape(raw_state))
    except json.JSONDecodeError:
        return None

    items = (
        (((payload.get("state") or {}).get("data") or {}).get("catalog") or {}).get("items")
        or []
    )
    if not isinstance(items, list):
        return None

    current_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        location = item.get("location")
        if not isinstance(location, dict):
            continue
        if location.get("isCurrent") is not True:
            continue
        if item_id is None:
            continue
        current_ids.add(str(item_id))

    return current_ids


def _profile_type(url_or_path: str) -> str:
    text = (url_or_path or "").lower()
    if "/brands/" in text:
        return "brands"
    if "/user/" in text:
        return "user"
    return "unknown"


def _extract_profile_url_from_source_link(source_link: str) -> str | None:
    raw = (source_link or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    query_suffix = _profile_query_suffix(parsed.query)

    low_path = path.lower()
    if "/brands/" in low_path:
        cleaned = f"{path}{query_suffix}"
    elif "/user/" in low_path:
        blocked = (
            "login",
            "registration",
            "logout",
            "settings",
            "review",
            "reviews",
            "rating",
            "ratings",
        )
        if any(f"/user/{slug}" in low_path for slug in blocked):
            return None
        profile_path = path if low_path.endswith("/profile") else f"{path}/profile"
        cleaned = f"{profile_path}{query_suffix}"
    else:
        return None

    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{cleaned}"
    return urljoin(AVITO_BASE, cleaned)


def _is_blocked(html_text: str) -> bool:
    lower = html_text[:3000].lower()
    return any(m in lower for m in _BLOCK_MARKERS)


def _is_proxy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _PROXY_ERROR_MARKERS)


def _is_browser_closed_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "target page, context or browser has been closed" in text
        or "target page/context/browser has been closed" in text
        or "browser has been closed" in text
        or "context has been closed" in text
        or "page has been closed" in text
        or "connection closed" in text
        or "pipe is being closed" in text
    )


def _should_treat_as_proxy_error(exc: Exception) -> bool:
    return (
        _is_proxy_error(exc)
        or isinstance(exc, asyncio.TimeoutError)
        or _is_browser_closed_error(exc)
    )


def _is_socks_proxy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "browser does not support socks5 proxy authentication" in text
        or "err_socks_connection_failed" in text
    )


def _has_valid_mfe_state(html_text: str) -> bool:
    soup = BeautifulSoup(html_text, "html.parser")
    state_script = soup.find("script", attrs={"data-mfe-state": "true"})
    if not state_script:
        return False
    raw_state = state_script.get_text()
    if not raw_state:
        return False
    try:
        payload = json.loads(html_mod.unescape(raw_state))
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("state"), dict)


def _looks_like_listing_html(html_text: str) -> bool:
    if not html_text or len(html_text) < 100:
        return False
    lower = html_text.lower()
    if "data-item-id=" in lower:
        return True
    if 'data-marker="item-title"' in lower or "data-marker='item-title'" in lower:
        return True
    return _has_valid_mfe_state(html_text)


def _looks_like_profile_html(html_text: str) -> bool:
    if not html_text or len(html_text) < 100:
        return False
    lower = html_text.lower()
    if 'data-marker="review(' in lower or "data-marker='review(" in lower:
        return True
    if "nextpage" in lower and "/ratings" in lower:
        return True
    if "/web/" in lower and "/ratings" in lower and "/user/" in lower:
        return True

    soup = BeautifulSoup(html_text, "html.parser")
    if soup.find(attrs={"data-marker": re.compile(r"^review\(\d+\)")}) is not None:
        return True
    return False


def _profile_query_suffix(raw_query: str) -> str:
    if not raw_query:
        return ""
    allowed = [
        (k, v)
        for k, v in parse_qsl(raw_query, keep_blank_values=True)
        if k in {"id", "iid", "src", "page_from"}
    ]
    if not allowed:
        return ""
    return f"?{urlencode(allowed, doseq=True)}"


# ---------------------------------------------------------------------------
# Seller review extraction helpers
# ---------------------------------------------------------------------------

def _extract_ratings_api_path(html: str) -> str | None:
    """Extract ratings API path from embedded JSON in the profile page."""
    decoded = html_mod.unescape(html)
    m = re.search(r'"nextPage"\s*:\s*"(/web/\d+/user/[^"]+/ratings[^"]*)"', decoded)
    if not m:
        return None
    path = m.group(1)
    path = re.sub(r"/web/\d+/", "/web/6/", path)
    return path


def _extract_user_hash(seller_path: str) -> str | None:
    """Extract user hash from a seller profile path like /user/abc123/profile."""
    m = re.search(r"/user/([a-zA-Z0-9_-]+)", seller_path)
    return m.group(1) if m else None


def _parse_ratings_json(data: dict, source_link: str) -> list[RawReview]:
    """Parse reviews from Avito ratings API JSON payload."""
    reviews: list[RawReview] = []

    for entry in data.get("entries", []):
        if not isinstance(entry, dict) or entry.get("type") != "rating":
            continue
        value = entry.get("value", {})
        if not isinstance(value, dict):
            continue

        text_parts = []
        for section in value.get("textSections", []):
            if isinstance(section, dict) and section.get("text"):
                text_parts.append(section["text"])
        text = " ".join(text_parts).strip()

        if not text or len(text) < 10:
            continue

        score = value.get("score")
        author = value.get("title")

        reviews.append(
            RawReview(
                source="avito",
                text=text[:500],
                rating=float(score) if score is not None else None,
                author=author,
                source_link=source_link,
            )
        )

    if reviews:
        return reviews

    items = (
        data.get("reviews")
        or data.get("ratings")
        or data.get("items")
        or (
            data.get("result", {}).get("reviews")
            if isinstance(data.get("result"), dict)
            else None
        )
        or []
    )

    for item in items:
        if not isinstance(item, dict):
            continue

        text = (item.get("text") or item.get("body") or item.get("comment") or "").strip()
        if not text or len(text) < 10:
            continue

        score = item.get("score") or item.get("rating")
        author_data = item.get("sender") or item.get("author") or item.get("user")
        author = None
        if isinstance(author_data, dict):
            author = author_data.get("name") or author_data.get("public_name")
        elif isinstance(author_data, str):
            author = author_data

        reviews.append(
            RawReview(
                source="avito",
                text=text[:500],
                rating=float(score) if score is not None else None,
                author=author,
                source_link=source_link,
            )
        )

    return reviews


def _extract_seller_path(html: str) -> str | None:
    """Extract seller profile path from ad page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    user_candidate: str | None = None

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "").strip()
        cleaned = _clean_seller_href(href)
        if not cleaned:
            continue
        low_cleaned = cleaned.lower()
        if "/brands/" in low_cleaned:
            return cleaned
        marker = (a.get("data-marker") or "").lower()
        if ("/user/" in low_cleaned or "seller" in marker) and not user_candidate:
            user_candidate = cleaned

    if user_candidate:
        return user_candidate

    for m in re.finditer(r'"/brands/([a-zA-Z0-9_-]{10,})', html):
        return f"/brands/{m.group(1)}"
    for m in re.finditer(r'"/user/([a-zA-Z0-9_-]{5,})', html):
        slug = m.group(1)
        if slug.lower() not in (
            "login",
            "registration",
            "logout",
            "settings",
            "review",
            "reviews",
            "rating",
            "ratings",
        ):
            return f"/user/{slug}/profile"

    return None


def _extract_rating_caption_path(html: str) -> str | None:
    """Extract review-page path from ad link data-marker='rating-caption/rating'."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find(
        "a",
        attrs={"data-marker": lambda v: isinstance(v, str) and v.startswith("rating-caption/rating")},
        href=True,
    )
    if not link:
        return None
    return _clean_seller_href(str(link.get("href") or ""))


def _clean_seller_href(href: str) -> str | None:
    """Normalize seller href to a user or brand profile path."""
    raw = (href or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    if not path:
        return None
    query_suffix = _profile_query_suffix(parsed.query)
    low_path = path.lower()

    if "/brands/" in low_path:
        return f"{path}{query_suffix}"

    if "/user/" not in low_path:
        return None

    blocked = (
        "login",
        "registration",
        "logout",
        "settings",
        "review",
        "reviews",
        "rating",
        "ratings",
    )
    if any(f"/user/{slug}" in low_path for slug in blocked):
        return None

    if not low_path.endswith("/profile"):
        path = f"{path}/profile"
    return f"{path}{query_suffix}"


def _parse_reviews_from_html(
    html: str,
    source_link: str,
    *,
    max_reviews: int = 200,
) -> list[RawReview]:
    """Extract reviews from Avito profile/ratings page HTML."""
    reviews: list[RawReview] = []
    seen: set[tuple[str, str, str]] = set()
    max_reviews = max(1, int(max_reviews))
    soup = BeautifulSoup(html, "html.parser")

    # Primary parser for canonical review blocks.
    for block in soup.find_all(attrs={"data-marker": re.compile(r"^review\(\d+\)$")}):
        review = _extract_review_from_marker_block(block, source_link)
        if not review:
            continue
        key = _review_dedupe_key(review)
        if key in seen:
            continue
        seen.add(key)
        reviews.append(review)
        if len(reviews) >= max_reviews:
            return reviews

    for pattern in [
        r"window\.__initialState__\s*=\s*({.+?});\s*</",
        r"window\.__initial_state__\s*=\s*({.+?});\s*</",
        r"window\.__DATA__\s*=\s*({.+?});\s*</",
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, RecursionError):
            continue
        for review in _walk_json_for_reviews(
            data,
            source_link,
            max_reviews=max_reviews,
        ):
            key = _review_dedupe_key(review)
            if key in seen:
                continue
            seen.add(key)
            reviews.append(review)
            if len(reviews) >= max_reviews:
                return reviews

    for block in soup.find_all(
        attrs={"data-marker": lambda v: v and ("review" in v.lower() or "rating" in v.lower())}
    ):
        marker = str(block.get("data-marker", ""))
        if re.match(r"^review\(\d+\)(/|$)", marker):
            continue
        review = _extract_single_review(block, source_link)
        if not review:
            continue
        key = _review_dedupe_key(review)
        if key in seen:
            continue
        seen.add(key)
        reviews.append(review)
        if len(reviews) >= max_reviews:
            return reviews

    for block in soup.find_all(attrs={"class": lambda v: v and _has_review_class(v)}):
        review = _extract_single_review(block, source_link)
        if not review:
            continue
        key = _review_dedupe_key(review)
        if key in seen:
            continue
        seen.add(key)
        reviews.append(review)
        if len(reviews) >= max_reviews:
            return reviews

    return reviews


def _has_review_class(classes) -> bool:
    """Check if element has a review-related CSS class."""
    if isinstance(classes, list):
        return any("review" in c.lower() or "rating-item" in c.lower() for c in classes)
    return "review" in str(classes).lower()


def _extract_single_review(block, source_link: str) -> RawReview | None:
    """Extract a single review from a DOM block."""
    text = block.get_text(strip=True)
    if not text or len(text) < 20 or len(text) > 2000:
        return None

    skip_words = [
        "войти",
        "зарегистрироваться",
        "avito",
        "продолжить",
        "подробнее",
        "показать ещё",
    ]
    if any(s in text.lower() for s in skip_words):
        return None

    return RawReview(
        source="avito",
        text=text[:500],
        rating=None,
        author=None,
        source_link=source_link,
    )


def _extract_review_from_marker_block(block, source_link: str) -> RawReview | None:
    """Extract a review from data-marker='review(N)' block."""
    text_el = block.find(attrs={"data-marker": lambda v: v and v.endswith("/text-section/text")})
    text = text_el.get_text(" ", strip=True) if text_el else ""
    if not text or len(text) < 5:
        return None

    author_el = block.find(attrs={"data-marker": lambda v: v and v.endswith("/header/title")})
    author = author_el.get_text(" ", strip=True) if author_el else None

    rating = None
    rating_meta = block.find("meta", attrs={"itemprop": "ratingValue"})
    if rating_meta and rating_meta.get("content"):
        rating = parse_float(str(rating_meta.get("content")))

    return RawReview(
        source="avito",
        text=text[:500],
        rating=rating,
        author=author,
        source_link=source_link,
    )


def _review_dedupe_key(review: RawReview) -> tuple[str, str, str]:
    text = re.sub(r"\s+", " ", (review.text or "").strip().lower())
    author = re.sub(r"\s+", " ", (review.author or "").strip().lower())
    rating = "" if review.rating is None else f"{review.rating:.2f}"
    return text, author, rating


def _walk_json_for_reviews(
    data,
    source_link: str,
    max_depth: int = 8,
    max_reviews: int = 200,
) -> list[RawReview]:
    """Recursively search embedded JSON state for review objects."""
    reviews: list[RawReview] = []
    max_reviews = max(1, int(max_reviews))

    def _walk(obj, depth: int = 0):
        if depth > max_depth or len(reviews) >= max_reviews:
            return
        if isinstance(obj, dict):
            text = obj.get("text") or obj.get("body") or obj.get("comment")
            if text and isinstance(text, str) and len(text.strip()) >= 10:
                score = obj.get("score") or obj.get("rating")
                author_data = obj.get("sender") or obj.get("author") or obj.get("user")
                author = None
                if isinstance(author_data, dict):
                    author = author_data.get("name") or author_data.get("public_name")
                elif isinstance(author_data, str):
                    author = author_data
                reviews.append(
                    RawReview(
                        source="avito",
                        text=text.strip()[:500],
                        rating=float(score) if score is not None else None,
                        author=author,
                        source_link=source_link,
                    )
                )
                return
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    _walk(val, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    _walk(item, depth + 1)

    _walk(data)
    return reviews
