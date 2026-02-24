"""ProxyManager — round-robin rotation from sx.org RU proxies, proxies.txt, or PROXY_URL."""

from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import urlparse

import structlog

from src.config import settings

log = structlog.get_logger(__name__)


class ProxyManager:
    """Thread-safe round-robin proxy rotator.

    Priority:
    1. SX.org RU proxy pool (fetched async on first use via ensure_loaded())
    2. PROXY_FILE (path to a file with one proxy per line)
    3. PROXY_URL  (single proxy string)
    4. No proxy   (direct connection)

    Proxy line format (each line in the file):
        http://user:pass@host:port
        socks5://user:pass@host:port
        host:port          ← treated as http://host:port
    """

    def __init__(self) -> None:
        self._proxies: list[str] = []
        self._index: int = 0
        self._lock = threading.Lock()
        self._sx_loaded = False
        self._sx_current: str | None = None
        self._sx_pool: list[str] = []
        self._load_static()

    def _load_static(self) -> None:
        """Load proxies from PROXY_FILE / PROXY_URL (sync, at import time)."""
        loaded: list[str] = []

        # 1. Try PROXY_FILE
        if settings.proxy_file:
            path = Path(settings.proxy_file)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        loaded.append(_normalise(line))
                log.info("proxy_manager.loaded_file", file=str(path), count=len(loaded))
            else:
                log.warning("proxy_manager.file_not_found", path=str(path))

        # 2. Fall back to single PROXY_URL
        if not loaded and settings.proxy_url:
            normalized = _normalise(settings.proxy_url)
            loaded = [normalized]
            log.info("proxy_manager.loaded_env", proxy=_proxy_log_hint(normalized))

        if not loaded:
            log.info("proxy_manager.no_static_proxy")

        self._proxies = loaded

    async def ensure_loaded(self) -> None:
        """Fetch sx.org RU proxy pool and prepend it to rotation."""
        if self._sx_loaded:
            return
        self._sx_loaded = True

        if not settings.sx_proxy_api_key:
            return

        from src.ai.sx_proxy import get_ru_proxy_pool

        pool_size = max(1, int(getattr(settings, "sx_proxy_ru_pool_size", 1)))
        ru_proxies = await get_ru_proxy_pool(pool_size)
        if ru_proxies:
            with self._lock:
                self._apply_sx_pool_locked(ru_proxies)
            log.info(
                "proxy_manager.sx_ru_loaded",
                pool_size=len(ru_proxies),
                primary=_proxy_log_hint(ru_proxies[0]),
                total=len(self._proxies),
            )
        else:
            log.warning("proxy_manager.sx_ru_failed", fallback_count=len(self._proxies))

    async def rotate_sx_ru(self) -> str | None:
        """Rotate to another SX RU proxy (or refresh via SX API as fallback)."""
        with self._lock:
            live_pool = [p for p in self._sx_pool if p in self._proxies]
            if len(live_pool) > 1:
                current = self._sx_current if self._sx_current in live_pool else live_pool[0]
                next_idx = (live_pool.index(current) + 1) % len(live_pool)
                next_proxy = live_pool[next_idx]
                if next_proxy in self._proxies:
                    self._proxies.remove(next_proxy)
                self._proxies.insert(0, next_proxy)
                self._sx_current = next_proxy
                self._index = 1
                log.info("proxy_manager.sx_ru_rotated", mode="pool_switch", proxy=_proxy_log_hint(next_proxy))
                return next_proxy

        from src.ai.sx_proxy import rotate_ru_proxy

        ru_proxy = await rotate_ru_proxy()
        if not ru_proxy:
            log.warning("proxy_manager.sx_ru_rotate_failed")
            return None

        with self._lock:
            if self._sx_current and self._sx_current in self._proxies:
                self._proxies.remove(self._sx_current)
            if ru_proxy in self._proxies:
                self._proxies.remove(ru_proxy)
            self._proxies.insert(0, ru_proxy)
            self._sx_pool = [ru_proxy] + [p for p in self._sx_pool if p != ru_proxy]
            self._sx_current = ru_proxy
            self._index = 0

        log.info(
            "proxy_manager.sx_ru_rotated",
            mode="api_refresh",
            proxy=_proxy_log_hint(ru_proxy),
            total=len(self._proxies),
        )
        return ru_proxy

    def _apply_sx_pool_locked(self, sx_proxies: list[str]) -> None:
        # Remove previously attached SX proxies first.
        for old in self._sx_pool:
            if old in self._proxies:
                self._proxies.remove(old)
        # Prepend new SX pool in declared order.
        ordered = list(dict.fromkeys(sx_proxies))
        for proxy in reversed(ordered):
            if proxy in self._proxies:
                self._proxies.remove(proxy)
            self._proxies.insert(0, proxy)
        self._sx_pool = ordered
        self._sx_current = ordered[0] if ordered else None
        self._index = 0

    def get_next(self) -> str | None:
        """Return the next proxy in rotation, or None if no proxies are configured."""
        if not self._proxies:
            return None
        with self._lock:
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
        return proxy

    def playwright_proxy(self) -> dict | None:
        """Return a Playwright-compatible proxy dict, or None.

        Playwright requires username/password as separate fields,
        not embedded in the URL (http://user:pass@host:port won't work).
        """
        url = self.get_next()
        if not url:
            return None
        parsed = urlparse(url)
        proxy: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy["username"] = parsed.username
        if parsed.password:
            proxy["password"] = parsed.password
        return proxy

    @property
    def count(self) -> int:
        return len(self._proxies)


def _normalise(raw: str) -> str:
    """Ensure the proxy string has a scheme prefix."""
    if "://" not in raw:
        return f"http://{raw}"
    return raw


def _proxy_log_hint(url: str) -> str:
    """Return a credentials-safe proxy hint for logs."""
    parsed = urlparse(_normalise(url))
    if not parsed.hostname:
        return "unknown"
    if parsed.port:
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    return f"{parsed.scheme}://{parsed.hostname}"


# Module-level singleton — shared across all collectors
proxy_manager = ProxyManager()
