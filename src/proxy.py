"""Per-runner proxy managers with isolated rotation pools."""

from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import structlog

from src.config import settings

log = structlog.get_logger(__name__)

DEFAULT_PROXY_SCOPE = "default"
_SCOPE_FILE_NAMES = {
    DEFAULT_PROXY_SCOPE: "default.txt",
    "avito": "avito.txt",
    "yandex": "yandex.txt",
    "twogis": "twogis.txt",
    "enrichment": "enrichment.txt",
    "website": "website.txt",
    "openai": "openai.txt",
}


class ProxyManager:
    """Thread-safe round-robin proxy rotator for a single scope."""

    def __init__(
        self,
        *,
        name: str = DEFAULT_PROXY_SCOPE,
        proxy_file: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self._name = (name or DEFAULT_PROXY_SCOPE).strip().lower()
        settings_proxy_file, settings_proxy_url = _scope_proxy_settings(self._name)
        self._proxy_file = settings_proxy_file if proxy_file is None else (proxy_file or "").strip()
        self._proxy_url = settings_proxy_url if proxy_url is None else (proxy_url or "").strip()
        self._resolved_file: Path | None = None
        self._proxies: list[str] = []
        self._index = 0
        self._lock = threading.Lock()
        self.reload()

    def reload(self) -> None:
        loaded: list[str] = []
        path = self._resolve_proxy_file()
        self._resolved_file = path

        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip().lstrip("\ufeff")
                if line and not line.startswith("#"):
                    loaded.append(_normalise(line))
            log.info("proxy_manager.loaded_file", scope=self._name, file=str(path), count=len(loaded))
        elif self._proxy_file:
            log.warning("proxy_manager.file_not_found", scope=self._name, path=str(path))

        if not loaded and self._proxy_url:
            loaded = [_normalise(self._proxy_url)]
            log.info("proxy_manager.loaded_env", scope=self._name, proxy=self._proxy_url)

        if not loaded:
            log.info("proxy_manager.no_proxy", scope=self._name, file=str(path) if path else None)

        with self._lock:
            self._proxies = loaded
            self._index = 0

    def _resolve_proxy_file(self) -> Path | None:
        explicit = (self._proxy_file or "").strip()
        if explicit:
            return Path(explicit)

        scope_file = _SCOPE_FILE_NAMES.get(self._name)
        if not scope_file:
            return None
        storage_dir_raw = getattr(settings, "proxy_storage_dir", None)
        if storage_dir_raw is None:
            storage_dir = Path("storage/proxies")
        else:
            storage_dir_text = str(storage_dir_raw).strip()
            if not storage_dir_text:
                return None
            storage_dir = Path(storage_dir_text)
        return storage_dir / scope_file

    def get_next(self) -> str | None:
        """Return the next proxy in rotation, or None if scope is empty."""
        if not self._proxies:
            return None
        with self._lock:
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
        return proxy

    def playwright_proxy(self, scheme: str | None = None) -> dict[str, str] | None:
        """Return a Playwright-compatible proxy dict, or None."""
        url = self.get_next()
        if not url:
            return None

        parsed = urlparse(url)
        selected_scheme = (scheme or parsed.scheme or "http").strip().lower()
        if not parsed.hostname:
            return {"server": url}

        if selected_scheme.startswith("socks"):
            auth = ""
            if parsed.username and parsed.password:
                user = quote(unquote(parsed.username), safe="")
                pwd = quote(unquote(parsed.password), safe="")
                auth = f"{user}:{pwd}@"
            return {"server": f"{selected_scheme}://{auth}{parsed.hostname}:{parsed.port}"}

        server = f"{selected_scheme}://{parsed.hostname}:{parsed.port}"
        proxy: dict[str, str] = {"server": server}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy

    @property
    def count(self) -> int:
        return len(self._proxies)

    @property
    def scope(self) -> str:
        return self._name

    @property
    def proxy_file(self) -> str | None:
        return str(self._resolved_file) if self._resolved_file else None


def _normalise(raw: str) -> str:
    """Ensure the proxy string has a scheme prefix."""
    if "://" not in raw:
        return f"http://{raw}"
    return raw


def proxy_log_hint(proxy: object) -> str:
    if isinstance(proxy, dict):
        server = proxy.get("server")
        if isinstance(server, str):
            return server
    if proxy is None:
        return "direct"
    return str(proxy)


def require_proxy_pool(manager: ProxyManager, *, purpose: str) -> None:
    if manager.count > 0:
        return
    raise RuntimeError(
        f"Proxy is required for {purpose} but no proxies are configured for scope={manager.scope}"
    )


def iter_proxy_urls(
    manager: ProxyManager,
    *,
    purpose: str,
    max_attempts: int | None = None,
):
    require_proxy_pool(manager, purpose=purpose)
    attempts = manager.count if max_attempts is None else min(manager.count, max(1, max_attempts))
    for _ in range(attempts):
        proxy_url = manager.get_next()
        if proxy_url:
            yield proxy_url


def iter_playwright_proxies(
    manager: ProxyManager,
    *,
    purpose: str,
    max_attempts: int | None = None,
    scheme: str | None = None,
):
    require_proxy_pool(manager, purpose=purpose)
    attempts = manager.count if max_attempts is None else min(manager.count, max(1, max_attempts))
    for _ in range(attempts):
        proxy = manager.playwright_proxy(scheme=scheme)
        if proxy:
            yield proxy


def _build_manager(scope: str) -> ProxyManager:
    return ProxyManager(name=(scope or DEFAULT_PROXY_SCOPE).strip().lower())


def _scope_proxy_settings(scope: str) -> tuple[str, str]:
    scope = (scope or DEFAULT_PROXY_SCOPE).strip().lower()
    if scope == "avito":
        return (
            str(getattr(settings, "avito_proxy_file", "") or "").strip(),
            str(getattr(settings, "avito_proxy_url", "") or "").strip(),
        )
    if scope == "yandex":
        return (
            str(getattr(settings, "yandex_proxy_file", "") or "").strip(),
            str(getattr(settings, "yandex_proxy_url", "") or "").strip(),
        )
    if scope == "twogis":
        return (
            str(getattr(settings, "twogis_proxy_file", "") or "").strip(),
            str(getattr(settings, "twogis_proxy_url", "") or "").strip(),
        )
    if scope == "enrichment":
        return (
            str(getattr(settings, "enrichment_proxy_file", "") or "").strip(),
            str(getattr(settings, "enrichment_proxy_url", "") or "").strip(),
        )
    if scope == "website":
        return (
            str(getattr(settings, "website_proxy_file", "") or "").strip(),
            str(getattr(settings, "website_proxy_url", "") or "").strip(),
        )
    if scope == "openai":
        return (
            str(getattr(settings, "openai_proxy_file", "") or "").strip(),
            str(getattr(settings, "openai_proxy_url", "") or "").strip(),
        )
    return (
        str(getattr(settings, "proxy_file", "") or "").strip(),
        str(getattr(settings, "proxy_url", "") or "").strip(),
    )


_MANAGERS: dict[str, ProxyManager] = {
    scope: _build_manager(scope)
    for scope in _SCOPE_FILE_NAMES
}


def get_proxy_manager(scope: str = DEFAULT_PROXY_SCOPE) -> ProxyManager:
    return _MANAGERS[(scope or DEFAULT_PROXY_SCOPE).strip().lower()]


def reload_proxy_managers() -> None:
    for manager in _MANAGERS.values():
        manager.reload()


proxy_manager = get_proxy_manager()
avito_proxy_manager = get_proxy_manager("avito")
yandex_proxy_manager = get_proxy_manager("yandex")
twogis_proxy_manager = get_proxy_manager("twogis")
enrichment_proxy_manager = get_proxy_manager("enrichment")
website_proxy_manager = get_proxy_manager("website")
openai_proxy_manager = get_proxy_manager("openai")
