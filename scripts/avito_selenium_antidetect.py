"""Headful Selenium anti-detect smoke run for Avito via SOCKS5 proxies.

Usage:
    python scripts/avito_selenium_antidetect.py --keyword "бурение скважин"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium_stealth import stealth
from webdriver_manager.chrome import ChromeDriverManager

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collectors.avito import _Socks5AuthBridge

AVITO_SEARCH = "https://www.avito.ru/omsk/predlozheniya_uslug"

PROXY_ERROR_MARKERS = (
    "err_socks_connection_failed",
    "err_proxy_connection_failed",
    "timeout",
    "timed out",
    "connection reset",
    "proxyconnect",
)


def _load_proxy_urls(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Proxy file not found: {path}")
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().lstrip("\ufeff")
        if line and not line.startswith("#"):
            lines.append(line)
    if not lines:
        raise RuntimeError(f"No proxies in file: {path}")
    return lines


def _is_proxy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in PROXY_ERROR_MARKERS)


def _parse_socks5_proxy(proxy_url: str) -> tuple[str, int, str, str]:
    parsed = urlparse(proxy_url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("socks5", "socks5h", "socks4", "socks4a"):
        raise RuntimeError(f"Proxy must be SOCKS, got: {proxy_url}")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError(f"Invalid proxy host/port: {proxy_url}")
    if not parsed.username or not parsed.password:
        raise RuntimeError(f"SOCKS auth required: {proxy_url}")
    return parsed.hostname, parsed.port, unquote(parsed.username), unquote(parsed.password)


def _run_selenium_once(
    *,
    local_socks_port: int,
    keyword: str,
    driver_path: str,
    timeout_s: int,
) -> tuple[str, str, int]:
    options = Options()
    options.page_load_strategy = "eager"
    options.add_argument(f"--proxy-server=socks5://127.0.0.1:{local_socks_port}")
    options.add_argument("--lang=ru-RU")
    options.add_argument("--window-size=1366,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    service = Service(executable_path=driver_path)
    driver = webdriver.Chrome(service=service, options=options)
    try:
        stealth(
            driver,
            languages=["ru-RU", "ru"],
            vendor="Google Inc.",
            platform="Win32",
            webgl_vendor="Intel Inc.",
            renderer="Intel Iris OpenGL Engine",
            fix_hairline=True,
        )
        driver.set_page_load_timeout(timeout_s)
        url = f"{AVITO_SEARCH}?q={quote_plus(keyword)}"
        driver.get(url)
        return driver.title, driver.current_url, len(driver.page_source or "")
    finally:
        driver.quit()


async def main() -> int:
    parser = argparse.ArgumentParser(description="Selenium anti-detect check for Avito with SOCKS5.")
    parser.add_argument("--keyword", default="бурение скважин")
    parser.add_argument("--proxy-file", default="storage/avito_socks5_proxies.txt")
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--cooldown-s", type=float, default=180.0)
    parser.add_argument("--request-interval-s", type=float, default=4.0)
    parser.add_argument("--timeout-s", type=int, default=60)
    args = parser.parse_args()

    proxy_urls = _load_proxy_urls(Path(args.proxy_file))
    driver_path = ChromeDriverManager().install()
    print(f"Using chromedriver: {driver_path}")

    cooldowns: dict[str, float] = {}
    last_request_ts = 0.0
    index = 0

    for attempt in range(1, args.max_attempts + 1):
        now = time.monotonic()
        elapsed = now - last_request_ts
        if elapsed < args.request_interval_s:
            await asyncio.sleep(args.request_interval_s - elapsed)

        proxy_url = proxy_urls[index % len(proxy_urls)]
        index += 1
        if cooldowns.get(proxy_url, 0.0) > time.monotonic():
            continue

        host, port, user, pwd = _parse_socks5_proxy(proxy_url)
        bridge = _Socks5AuthBridge(host=host, port=port, username=user, password=pwd)

        print(f"Attempt {attempt}/{args.max_attempts} with {host}:{port}")
        await bridge.start()
        try:
            title, url, html_len = await asyncio.wait_for(
                asyncio.to_thread(
                    _run_selenium_once,
                    local_socks_port=bridge.listen_port,
                    keyword=args.keyword,
                    driver_path=driver_path,
                    timeout_s=args.timeout_s,
                ),
                timeout=max(120, args.timeout_s + 30),
            )
            print("SUCCESS")
            print(f"title={title}")
            print(f"url={url}")
            print(f"html_len={html_len}")
            return 0
        except Exception as exc:
            is_proxy = _is_proxy_error(exc)
            if is_proxy:
                cooldowns[proxy_url] = time.monotonic() + args.cooldown_s
            print(f"ERROR attempt={attempt} proxy_error={is_proxy} error={exc}")
        finally:
            last_request_ts = time.monotonic()
            await bridge.close()

    print("FAILED: all attempts exhausted")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
