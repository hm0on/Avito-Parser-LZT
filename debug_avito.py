"""
Диагностика Avito коллектора — httpx + HTML parsing.
Показывает что реально парсится с сайта.

Запуск:
    python3 debug_avito.py          # делает реальный запрос через прокси
    python3 debug_avito.py --local  # парсит уже сохранённый avito_last_page.html
"""

import asyncio
import sys
from pathlib import Path
from urllib.parse import quote_plus

import httpx

from src.collectors.avito import _is_blocked, _parse_html
from src.proxy import proxy_manager

URL = "https://www.avito.ru/omsk/uslugi?q=" + quote_plus("бурение скважин")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://www.google.ru/",
    "Upgrade-Insecure-Requests": "1",
}


async def fetch(url: str) -> str | None:
    proxy_url = proxy_manager.get_next()
    print(f"Прокси: {proxy_url or 'не настроен'}")
    print(f"URL: {url}\n")
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url or None,
            headers=_HEADERS,
            timeout=30.0,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            print(f"HTTP {resp.status_code}  |  размер: {len(resp.text):,} символов")
            return resp.text
    except Exception as exc:
        print(f"Ошибка запроса: {exc}")
        return None


def analyse(html_text: str, keyword: str = "бурение скважин") -> None:
    GREEN = "\033[92m"
    RED   = "\033[91m"
    RESET = "\033[0m"

    # Блокировка?
    if _is_blocked(html_text):
        print(f"\n{RED}✗ Страница заблокирована (captcha / firewall){RESET}")
        Path("avito_last_page.html").write_text(html_text, encoding="utf-8")
        print("  HTML сохранён: avito_last_page.html")
        return

    print(f"\n{GREEN}✓ Блокировки нет{RESET}")

    # Парсинг
    companies = _parse_html(html_text, keyword)
    print(f"{GREEN}✓ Компаний найдено: {len(companies)}{RESET}")

    if not companies:
        print(f"\n{RED}  Карточек не найдено. Сохраняем HTML для анализа.{RESET}")
        Path("avito_last_page.html").write_text(html_text, encoding="utf-8")
        print("  avito_last_page.html сохранён")
        return

    # Показываем первые 5
    print(f"\n{'─'*60}")
    for i, c in enumerate(companies[:5], 1):
        print(f"\n[{i}] {c.name_raw}")
        print(f"    ID:      {c.source_id}")
        print(f"    Ссылка:  {c.source_link}")
        print(f"    Адрес:   {c.addresses[0] if c.addresses else '—'}")
        print(f"    Рейтинг: {c.average_rating or '—'}  |  Отзывов: {c.reviews_count or '—'}")

    if len(companies) > 5:
        print(f"\n  ... и ещё {len(companies) - 5} компаний")

    # Статистика качества
    print(f"\n{'─'*60}")
    with_rating   = sum(1 for c in companies if c.average_rating)
    with_reviews  = sum(1 for c in companies if c.reviews_count)
    with_address  = sum(1 for c in companies if c.addresses)
    print(f"Статистика по {len(companies)} компаниям:")
    print(f"  С рейтингом:  {with_rating}/{len(companies)}")
    print(f"  С отзывами:   {with_reviews}/{len(companies)}")
    print(f"  С адресом:    {with_address}/{len(companies)}")


async def main() -> None:
    local_mode = "--local" in sys.argv

    if local_mode:
        path = Path("avito_last_page.html")
        if not path.exists():
            print("Файл avito_last_page.html не найден. Запусти без --local для реального запроса.")
            return
        print(f"Режим --local: читаем {path}")
        html_text = path.read_text(encoding="utf-8")
        print(f"Размер: {len(html_text):,} символов")
    else:
        html_text = await fetch(URL)
        if not html_text:
            return
        Path("avito_last_page.html").write_text(html_text, encoding="utf-8")

    analyse(html_text)


asyncio.run(main())
