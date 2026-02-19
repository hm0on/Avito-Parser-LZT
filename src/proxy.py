"""ProxyManager — round-robin rotation from proxies.txt or single PROXY_URL."""

from __future__ import annotations

import threading
from pathlib import Path

import structlog

from src.config import settings

log = structlog.get_logger(__name__)


class ProxyManager:
    """Thread-safe round-robin proxy rotator.

    Priority:
    1. PROXY_FILE (path to a file with one proxy per line)
    2. PROXY_URL  (single proxy string)
    3. No proxy   (direct connection)

    Proxy line format (each line in the file):
        http://user:pass@host:port
        socks5://user:pass@host:port
        host:port          ← treated as http://host:port
    """

    def __init__(self) -> None:
        self._proxies: list[str] = []
        self._index: int = 0
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
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
            log.info("proxy_manager.no_proxy")

        self._proxies = loaded

    def get_next(self) -> str | None:
        """Return the next proxy in rotation, or None if no proxies are configured."""
        if not self._proxies:
            return None
        with self._lock:
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
        return proxy

    def playwright_proxy(self) -> dict | None:
        """Return a Playwright-compatible proxy dict, or None."""
        url = self.get_next()
        return {"server": url} if url else None

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
