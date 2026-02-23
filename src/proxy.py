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
    1. SX.org RU proxy (fetched async on first use via ensure_loaded())
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
            loaded = [settings.proxy_url]
            log.info("proxy_manager.loaded_env", proxy=settings.proxy_url)

        if not loaded:
            log.info("proxy_manager.no_static_proxy")

        self._proxies = loaded

    async def ensure_loaded(self) -> None:
        """Fetch sx.org RU proxy and prepend to the pool (async, call before pipeline)."""
        if self._sx_loaded:
            return
        self._sx_loaded = True

        if not settings.sx_proxy_api_key:
            return

        from src.ai.sx_proxy import get_ru_proxy

        ru_proxy = await get_ru_proxy()
        if ru_proxy:
            with self._lock:
                # Prepend sx.org proxy so it has priority
                if ru_proxy not in self._proxies:
                    self._proxies.insert(0, ru_proxy)
            log.info(
                "proxy_manager.sx_ru_loaded",
                proxy=ru_proxy.split("@")[-1],
                total=len(self._proxies),
            )
        else:
            log.warning("proxy_manager.sx_ru_failed", fallback_count=len(self._proxies))

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


# Module-level singleton — shared across all collectors
proxy_manager = ProxyManager()
