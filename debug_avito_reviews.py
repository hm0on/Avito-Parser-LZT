"""
Диагностика парсинга отзывов Авито — полный API flow.

Тестирует: профиль → извлечение API URL → запрос ratings API → парсинг JSON.
Использует handle_block() для обновления cookies при 429.
Пробует API версии 6, 5, 4.

Запуск:
    python3 debug_avito_reviews.py [URL профиля]
"""
import asyncio
import json
import re
import sys

import httpx

from src.collectors.avito import (
    AVITO_BASE,
    _HEADERS,
    _build_cookies_provider,
    _extract_ratings_api_path,
    _extract_user_hash,
    _is_blocked,
    _parse_ratings_json,
)
from src.proxy import proxy_manager


async def _fetch_with_retry(cookies_provider, url, headers=None, max_attempts=3):
    """Fetch URL with retry + cookie renewal on 429."""
    resp = None
    for attempt in range(1, max_attempts + 1):
        cookies = cookies_provider.get() if cookies_provider else {}
        proxy_url = proxy_manager.get_next()

        async with httpx.AsyncClient(
            proxy=proxy_url or None,
            headers=headers or _HEADERS,
            cookies=cookies,
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            print(f"    Attempt {attempt}: status={resp.status_code}, "
                  f"size={len(resp.text)} chars")

            if resp.status_code == 200:
                if _is_blocked(resp.text):
                    print("    БЛОКИРОВКА: captcha/firewall")
                    if cookies_provider:
                        print("    → handle_block() → обновляю cookies...")
                        cookies_provider.handle_block()
                    await asyncio.sleep(5 * attempt)
                    continue
                return resp

            if resp.status_code == 429:
                print("    Rate limited (429)")
                if cookies_provider:
                    print("    → handle_block() → обновляю cookies...")
                    cookies_provider.handle_block()
                wait = 5 * attempt
                print(f"    → жду {wait} сек...")
                await asyncio.sleep(wait)
                continue

            print(f"    HTTP {resp.status_code}")
            return resp

    return resp


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.avito.ru/user/98d23a60dedea0a5d2add4990dd57400/profile"
    )

    cookies_provider = _build_cookies_provider()
    proxy_url = proxy_manager.get_next()
    cookies = cookies_provider.get() if cookies_provider else {}

    print(f"\n{'='*70}")
    print(f"URL: {url}")
    print(f"Proxy: {proxy_url or 'нет'}")
    print(f"Cookies: {len(cookies)} шт" if cookies else "Cookies: нет")
    print(f"{'='*70}\n")

    # ── Step 1: Fetch the profile page (with retry + handle_block) ────────
    print("[1] Загружаю страницу профиля...")
    resp = await _fetch_with_retry(cookies_provider, url)

    if not resp or resp.status_code != 200:
        print(f"    ОШИБКА: не удалось загрузить (status={resp.status_code if resp else 'None'})")
        return

    html = resp.text
    print(f"    OK: {len(html)} chars")

    # ── Step 2: Extract ratings API path ──────────────────────────────────
    print("\n[2] Ищу ratings API URL в embedded JSON...")
    api_path = _extract_ratings_api_path(html)
    if api_path:
        print(f"    Найден: {api_path}")
    else:
        print("    Не найден в JSON, конструирую вручную...")
        user_hash = _extract_user_hash(url)
        if user_hash:
            api_path = f"/web/6/user/{user_hash}/ratings"
            print(f"    Сконструирован: {api_path}")
        else:
            print("    ОШИБКА: не удалось определить user hash")
            return

    # ── Step 3: Try multiple API versions ─────────────────────────────────
    print(f"\n[3] Пробую ratings API версии 6, 5, 4 (задержка 3 сек)...")
    await asyncio.sleep(3)

    api_headers = {
        **_HEADERS,
        "Accept": "application/json, text/plain, */*",
        "Referer": url,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    base_path = re.sub(r"/web/\d+/", "/web/{ver}/", api_path)

    for ver in [6, 5, 4]:
        path = base_path.replace("{ver}", str(ver))
        ratings_url = f"{AVITO_BASE}{path}"
        print(f"\n  → v{ver}: {ratings_url}")

        resp = await _fetch_with_retry(cookies_provider, ratings_url, headers=api_headers)

        if not resp:
            continue

        if resp.status_code == 404:
            print(f"    404 — версия {ver} не существует")
            await asyncio.sleep(1)
            continue

        if resp.status_code != 200:
            print(f"    Не удалось (status={resp.status_code})")
            await asyncio.sleep(2)
            continue

        # ── Step 4: Parse JSON ────────────────────────────────────────────
        ct = resp.headers.get("content-type", "")
        if "json" not in ct:
            print(f"    Не JSON (content-type: {ct})")
            print(f"    Body: {resp.text[:300]}")
            continue

        data = resp.json()
        print(f"    JSON keys: {list(data.keys())}")

        for key in data:
            val = data[key]
            if isinstance(val, list):
                print(f"    data['{key}'] = list[{len(val)}]")
                if val and isinstance(val[0], dict):
                    print(f"      [0].keys = {list(val[0].keys())}")
            elif isinstance(val, dict):
                print(f"    data['{key}'] = dict({list(val.keys())[:8]})")
            else:
                print(f"    data['{key}'] = {str(val)[:100]}")

        reviews = _parse_ratings_json(data, url)
        print(f"\n    _parse_ratings_json → {len(reviews)} отзывов")

        # ── Step 5: Show results ──────────────────────────────────────────
        print(f"\n{'='*70}")
        if reviews:
            print(f"РЕЗУЛЬТАТ (v{ver}): {len(reviews)} отзывов найдено\n")
            for i, rv in enumerate(reviews[:5], 1):
                print(f"  [{i}] rating={rv.rating}, author={rv.author}")
                print(f"      {rv.text[:120]}")
                print()
        else:
            print(f"РЕЗУЛЬТАТ (v{ver}): Отзывы НЕ найдены в JSON")
            print("JSON ответ для анализа:")
            print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
        print(f"{'='*70}\n")
        return  # Success — no need to try more versions

    print(f"\n{'='*70}")
    print("РЕЗУЛЬТАТ: Ни одна версия API не вернула данные")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    asyncio.run(main())
