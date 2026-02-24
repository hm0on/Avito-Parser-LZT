"""SX.org proxy manager — provides RU proxies for scraping and US proxy for OpenAI."""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import structlog

from src.config import settings

log = structlog.get_logger(__name__)

_SX_API = "https://api.sx.org/v2/proxy"

# Per-country cache: country_code → primary proxy URL
_cache: dict[str, str] = {}
# Per-country proxy pool cache
_pool_cache: dict[str, list[str]] = {}
# Per-country port IDs
_port_ids: dict[str, list[int]] = {}
_rotate_idx: dict[str, int] = {}


async def get_proxy(country: str = "RU", name_suffix: str = "scraping") -> str | None:
    """Return an sx.org proxy URL for the given country.

    Args:
        country: ISO country code (e.g. "RU", "US").
        name_suffix: label for the port (for sx.org dashboard).

    Returns:
        Proxy URL like http://user:pass@host:port, or None.
    """
    if country in _cache:
        return _cache[country]

    pool = await get_proxy_pool(country=country, size=1, name_suffix=name_suffix)
    if not pool:
        return None
    return pool[0]


async def get_proxy_pool(
    country: str = "RU",
    size: int = 1,
    name_suffix: str = "scraping",
) -> list[str]:
    """Return a list of sx.org proxies for the given country."""
    size = max(1, int(size))
    cached = _pool_cache.get(country, [])
    if len(cached) >= size:
        return cached[:size]

    api_key = settings.sx_proxy_api_key
    if not api_key:
        log.warning("sx_proxy.skip", reason="SX_PROXY_API_KEY not set")
        return []

    proxies: list[str] = []
    known_ids: list[int] = []

    # Try cached + saved port IDs
    candidate_ids = list(dict.fromkeys([*(_port_ids.get(country, [])), *_get_saved_port_ids(country)]))
    for saved_id in candidate_ids:
        if saved_id in known_ids:
            continue
        url = await _get_port_info(api_key, saved_id)
        if url and url not in proxies:
            proxies.append(url)
            known_ids.append(saved_id)

    _port_ids[country] = known_ids

    if len(proxies) >= size:
        _pool_cache[country] = proxies
        _cache[country] = proxies[0]
        return proxies[:size]

    if not settings.sx_proxy_allow_create:
        if proxies:
            _pool_cache[country] = proxies
            _cache[country] = proxies[0]
            return proxies
        log.warning("sx_proxy.create_blocked", reason="SX_PROXY_ALLOW_CREATE=false", country=country)
        return []

    # Create missing ports until we reach requested pool size
    for idx in range(len(proxies), size):
        url = await _create_port(api_key, country, f"{name_suffix}-{idx + 1}")
        if url and url not in proxies:
            proxies.append(url)

    if proxies:
        _pool_cache[country] = proxies
        _cache[country] = proxies[0]
    return proxies


def _get_saved_port_ids(country: str) -> list[int]:
    """Get saved port IDs from config for the given country."""
    if country == "RU":
        ids: list[int] = []
        raw = (settings.sx_proxy_port_ids_ru or "").strip()
        if raw:
            for token in raw.split(","):
                token = token.strip()
                if not token:
                    continue
                if token.isdigit():
                    ids.append(int(token))
                else:
                    log.warning("sx_proxy.bad_port_id", country=country, value=token)
        if settings.sx_proxy_port_id_ru > 0:
            ids.append(settings.sx_proxy_port_id_ru)
        return list(dict.fromkeys(ids))
    if country == "US":
        return [settings.sx_proxy_port_id_us] if settings.sx_proxy_port_id_us > 0 else []
    return []


async def get_ru_proxy() -> str | None:
    """Shortcut: get Russian proxy for scraping."""
    return await get_proxy("RU", "scraping")


async def get_ru_proxy_pool(size: int) -> list[str]:
    """Shortcut: get a RU proxy pool for scraping."""
    return await get_proxy_pool("RU", size=size, name_suffix="scraping")


async def get_us_proxy() -> str | None:
    """Shortcut: get US proxy for OpenAI API."""
    return await get_proxy("US", "openai")


async def rotate_ru_proxy() -> str | None:
    """Force rotate RU proxy endpoint via SX API and return fresh credentials."""
    return await rotate_proxy("RU", "scraping")


async def rotate_proxy(country: str = "RU", name_suffix: str = "scraping") -> str | None:
    """Force refresh existing SX port (or create a new one) and return proxy URL."""
    api_key = settings.sx_proxy_api_key
    if not api_key:
        log.warning("sx_proxy.rotate.skip", reason="SX_PROXY_API_KEY not set", country=country)
        return None

    port_ids = _port_ids.get(country) or _get_saved_port_ids(country)
    if port_ids:
        idx = _rotate_idx.get(country, 0) % len(port_ids)
        _rotate_idx[country] = idx + 1
        port_id = port_ids[idx]

        refreshed = await _refresh_port(api_key, port_id)
        if refreshed:
            url = await _get_port_info(api_key, port_id)
            if url:
                _cache[country] = url
                pool = _pool_cache.get(country, [])
                if url in pool:
                    pool.remove(url)
                pool.insert(0, url)
                _pool_cache[country] = pool
                _port_ids[country] = list(dict.fromkeys(port_ids))
                return url
        log.warning("sx_proxy.rotate.refresh_failed", country=country, port_id=port_id)

    if not settings.sx_proxy_allow_create:
        log.warning("sx_proxy.rotate.create_blocked", reason="SX_PROXY_ALLOW_CREATE=false", country=country)
        return None

    url = await _create_port(api_key, country, name_suffix)
    if url:
        _cache[country] = url
        pool = _pool_cache.get(country, [])
        if url in pool:
            pool.remove(url)
        pool.insert(0, url)
        _pool_cache[country] = pool
    return url


async def _create_port(api_key: str, country: str, name_suffix: str) -> str | None:
    """Create a new sx.org proxy port and return proxy URL."""
    endpoint = f"{_SX_API}/create-port?apiKey={api_key}"
    payload = {
        "country_code": country,
        "type_id": 1,              # KEEP PROXY
        "proxy_type_id": 2,        # ALL (residential + datacenter)
        "name": f"avito-parser-{name_suffix}",
        "server_port_type_id": 0,  # SHARED
        "count": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json()

        log.info("sx_proxy.port_created", country=country, response_keys=list(data.keys()))

        proxy_url = _extract_proxy_url(data)
        if proxy_url:
            port_id = _extract_port_id(data)
            if port_id:
                ids = _port_ids.get(country, [])
                if port_id not in ids:
                    ids.append(port_id)
                _port_ids[country] = ids
                env_var = "SX_PROXY_PORT_IDS_RU" if country == "RU" else f"SX_PROXY_PORT_ID_{country}"
                log.info(
                    "sx_proxy.save_port_id",
                    country=country,
                    port_id=port_id,
                    hint=(
                        f"Add {env_var}={','.join(str(i) for i in ids)} to .env to reuse"
                        if country == "RU"
                        else f"Add {env_var}={port_id} to .env to reuse"
                    ),
                )
            return proxy_url

        log.error("sx_proxy.create_port.no_credentials", country=country, data=data)
        return None

    except Exception as exc:
        log.error("sx_proxy.create_port.error", country=country, error=str(exc))
        return None


async def _get_port_info(api_key: str, port_id: int) -> str | None:
    """Get proxy credentials for an existing port."""
    endpoint = f"{_SX_API}/port-info?apiKey={api_key}&id={port_id}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(endpoint)
            resp.raise_for_status()
            raw = resp.json()
            # API sometimes wraps payload in "data" or "message"
            data = raw.get("data") or raw.get("message") or raw

        proxy_url = _extract_proxy_url(data)
        if proxy_url:
            log.info("sx_proxy.port_reused", port_id=port_id)
            return proxy_url

        log.warning("sx_proxy.port_info.no_credentials", port_id=port_id, data=data)
        return None

    except Exception as exc:
        log.warning("sx_proxy.port_info.error", port_id=port_id, error=str(exc))
        return None


async def _refresh_port(api_key: str, port_id: int) -> bool:
    """Trigger external IP refresh for a given SX port ID."""
    endpoint = f"{_SX_API}/refresh/{port_id}?apiKey={api_key}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(endpoint)
            resp.raise_for_status()
            data = resp.json()
        ok = bool(data.get("success", True))
        if ok:
            log.info("sx_proxy.port_refreshed", port_id=port_id)
            return True
        log.warning("sx_proxy.port_refresh_failed", port_id=port_id, data=data)
        return False
    except Exception as exc:
        log.warning("sx_proxy.port_refresh_error", port_id=port_id, error=str(exc))
        return False


def _extract_port_id(data: dict) -> int | None:
    """Extract port ID from sx.org API response."""
    pid = data.get("id") or data.get("port_id")
    if pid:
        return int(pid)
    d = data.get("data") or data.get("message") or data.get("info")
    if isinstance(d, list) and d:
        d = d[0]
    if isinstance(d, dict):
        pid = d.get("id") or d.get("port_id")
        if not pid and "info" in d and isinstance(d["info"], dict):
            pid = d["info"].get("id") or d["info"].get("port_id")
        if pid:
            return int(pid)
    return None


def _extract_proxy_url(data: dict | list | Mapping) -> str | None:
    """Extract proxy URL from sx.org API response.

    Tries multiple possible response formats.
    """
    if isinstance(data, list):
        if not data:
            return None
        first = data[0]
        return _extract_proxy_url(first) if isinstance(first, (dict, list, Mapping)) else None

    if not isinstance(data, dict):
        if isinstance(data, Mapping):
            data = dict(data)
        else:
            return None

    # Unwrap message/data wrappers
    if "message" in data and isinstance(data["message"], dict):
        data = data["message"]
    elif "message" in data and isinstance(data["message"], list) and data["message"]:
        first = data["message"][0]
        if isinstance(first, dict):
            data = first
    if "data" in data and isinstance(data["data"], dict):
        # Prefer proxy/info inside data
        data = data["data"]
    elif "data" in data and isinstance(data["data"], list) and data["data"]:
        first = data["data"][0]
        if isinstance(first, dict):
            data = first

    # Format 1: direct fields
    host = data.get("host") or data.get("server") or data.get("proxy_host") or data.get("ip")
    port = data.get("port") or data.get("proxy_port")
    username = data.get("username") or data.get("login") or data.get("proxy_login")
    password = data.get("password") or data.get("proxy_password")

    # Format 2: nested under "proxy" key
    if not host and "proxy" in data:
        p = data["proxy"]
        host = p.get("host") or p.get("ip")
        port = p.get("port")
        username = username or p.get("username") or p.get("login")
        password = password or p.get("password")
        auth = p.get("auth") if isinstance(p, dict) else None
        if isinstance(auth, dict):
            username = username or auth.get("login") or auth.get("username")
            password = password or auth.get("password")

    # Format 3: nested under "data" key (dict or list)
    if not host and "data" in data:
        d = data["data"]
        if isinstance(d, list) and d:
            d = d[0]
        if isinstance(d, dict):
            host = d.get("host") or d.get("server") or d.get("proxy_host") or d.get("ip")
            port = port or d.get("port") or d.get("proxy_port")
            username = username or d.get("username") or d.get("login")
            password = password or d.get("password")

    # Format 4: full proxy URL string
    for key in ("proxy_url", "proxy", "url", "connection_string"):
        val = data.get(key)
        if isinstance(val, str) and "://" in val:
            log.info("sx_proxy.extracted_url", key=key)
            return val

    if host and port:
        if username and password:
            url = f"http://{username}:{password}@{host}:{port}"
        else:
            url = f"http://{host}:{port}"
        log.info("sx_proxy.built_url", host=host, port=port, has_auth=bool(username))
        return url

    return None
