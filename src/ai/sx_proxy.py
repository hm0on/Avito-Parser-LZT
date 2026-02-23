"""SX.org proxy manager — provides RU proxies for scraping and US proxy for OpenAI."""

from __future__ import annotations

import httpx
import structlog

from src.config import settings

log = structlog.get_logger(__name__)

_SX_API = "https://api.sx.org/v2/proxy"

# Per-country cache: country_code → proxy URL
_cache: dict[str, str] = {}
# Per-country port IDs
_port_ids: dict[str, int] = {}


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

    api_key = settings.sx_proxy_api_key
    if not api_key:
        log.warning("sx_proxy.skip", reason="SX_PROXY_API_KEY not set")
        return None

    # Try saved port IDs from config
    saved_id = _get_saved_port_id(country)
    if saved_id:
        url = await _get_port_info(api_key, saved_id)
        if url:
            _cache[country] = url
            _port_ids[country] = saved_id
            return url

    # Create a new port
    url = await _create_port(api_key, country, name_suffix)
    if url:
        _cache[country] = url
    return url


def _get_saved_port_id(country: str) -> int:
    """Get saved port ID from config for the given country."""
    if country == "RU":
        return settings.sx_proxy_port_id_ru
    elif country == "US":
        return settings.sx_proxy_port_id_us
    return 0


async def get_ru_proxy() -> str | None:
    """Shortcut: get Russian proxy for scraping."""
    return await get_proxy("RU", "scraping")


async def get_us_proxy() -> str | None:
    """Shortcut: get US proxy for OpenAI API."""
    return await get_proxy("US", "openai")


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
            port_id = data.get("id") or data.get("port_id")
            if port_id:
                _port_ids[country] = port_id
                env_var = f"SX_PROXY_PORT_ID_{country}"
                log.info(
                    "sx_proxy.save_port_id",
                    country=country,
                    port_id=port_id,
                    hint=f"Add {env_var}={port_id} to .env to reuse",
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
            data = resp.json()

        proxy_url = _extract_proxy_url(data)
        if proxy_url:
            log.info("sx_proxy.port_reused", port_id=port_id)
            return proxy_url

        log.warning("sx_proxy.port_info.no_credentials", port_id=port_id, data=data)
        return None

    except Exception as exc:
        log.warning("sx_proxy.port_info.error", port_id=port_id, error=str(exc))
        return None


def _extract_proxy_url(data: dict) -> str | None:
    """Extract proxy URL from sx.org API response.

    Tries multiple possible response formats.
    """
    # Format 1: direct fields
    host = data.get("host") or data.get("proxy_host") or data.get("ip")
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

    # Format 3: nested under "data" key
    if not host and "data" in data and isinstance(data["data"], dict):
        d = data["data"]
        host = d.get("host") or d.get("proxy_host") or d.get("ip")
        port = d.get("port") or d.get("proxy_port")
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
