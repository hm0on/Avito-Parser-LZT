"""
Smoke-test скрипт — быстрая проверка коллекторов и реестров без БД.

Запуск:
    python smoke_test.py                        # все компоненты
    python smoke_test.py --only twogis          # только 2GIS
    python smoke_test.py --only avito,yandex    # несколько
    python smoke_test.py --only fssp            # только ФССП

Требования:
    pip install -r requirements.txt
    playwright install chromium

Прокси НЕ нужен — скрипт работает без него.
БД НЕ нужна — результаты выводятся в консоль.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass

# ── Keyword for test ──────────────────────────────────────────────────────────
TEST_KEYWORD = "бурение скважин"

# ── FSSP test company (widely known, should produce results) ──────────────────
FSSP_TEST_NAME = "Рога и Копыта"  # нейтральное имя для теста формы

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def ok(msg: str)   -> str: return f"{GREEN}✓{RESET} {msg}"
def warn(msg: str) -> str: return f"{YELLOW}⚠{RESET} {msg}"
def fail(msg: str) -> str: return f"{RED}✗{RESET} {msg}"
def head(msg: str) -> str: return f"\n{BOLD}{msg}{RESET}"


@dataclass
class Result:
    name: str
    success: bool
    detail: str
    elapsed: float


results: list[Result] = []


def record(name: str, success: bool, detail: str, elapsed: float) -> None:
    results.append(Result(name, success, detail, elapsed))
    icon = ok(name) if success else fail(name)
    print(f"  {icon}  {detail}  [{elapsed:.1f}s]")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _companies_summary(companies) -> str:
    if not companies:
        return "0 компаний"
    names = [c.name_raw or "?" for c in companies[:3]]
    extra = f" (+{len(companies)-3} ещё)" if len(companies) > 3 else ""
    return f"{len(companies)} компаний: {', '.join(names)}{extra}"


# ── Individual tests ──────────────────────────────────────────────────────────

async def test_twogis() -> None:
    print(head("2GIS — Playwright key capture + catalog API"))
    from src.collectors.twogis import TwoGisCollector

    t0 = time.monotonic()
    try:
        collector = TwoGisCollector()
        companies = await collector.collect(TEST_KEYWORD)
        elapsed = time.monotonic() - t0

        if companies:
            record("2gis.collect", True, _companies_summary(companies), elapsed)
            c = companies[0]
            print(f"    Первая компания: {c.name_raw}")
            print(f"    Телефоны: {c.phones}")
            print(f"    Адрес: {c.addresses}")
            print(f"    Рейтинг: {c.average_rating}  Отзывов (текстов): {len(c.reviews)}")
        else:
            record("2gis.collect", False, "вернул 0 компаний", elapsed)
    except Exception as exc:
        record("2gis.collect", False, str(exc)[:120], time.monotonic() - t0)


async def test_avito() -> None:
    print(head("Avito — httpx + HTML parser"))
    from src.collectors.avito import AvitoCollector

    t0 = time.monotonic()
    try:
        collector = AvitoCollector()
        companies = await collector.collect(TEST_KEYWORD)
        elapsed = time.monotonic() - t0

        if companies:
            record("avito.collect", True, _companies_summary(companies), elapsed)
            c = companies[0]
            with_rating  = sum(1 for x in companies if x.average_rating)
            with_address = sum(1 for x in companies if x.addresses)
            print(f"    Первая компания: {c.name_raw}")
            print(f"    Ссылка:   {c.source_link}")
            print(f"    Адрес:    {c.addresses[0] if c.addresses else '—'}")
            print(f"    Рейтинг:  {c.average_rating or '—'}  (кол-во отзывов: {c.reviews_count or '—'})")
            print(f"    Статистика: рейтинг у {with_rating}/{len(companies)}, адрес у {with_address}/{len(companies)}")
            print(f"    Примечание: тексты отзывов берутся из Stage 2 (Flamp/VK/Otzovik)")
        else:
            record("avito.collect", False, "вернул 0 компаний", elapsed)
    except Exception as exc:
        record("avito.collect", False, str(exc)[:120], time.monotonic() - t0)


async def test_yandex() -> None:
    print(head("Яндекс.Карты — Playwright scraper"))
    from src.collectors.yandex import YandexCollector

    t0 = time.monotonic()
    try:
        collector = YandexCollector()
        companies = await collector.collect(TEST_KEYWORD)
        elapsed = time.monotonic() - t0

        if companies:
            record("yandex.collect", True, _companies_summary(companies), elapsed)
            c = companies[0]
            print(f"    Первая компания: {c.name_raw}")
            print(f"    Отзывов: {len(c.reviews)}")
        else:
            record("yandex.collect", False, "вернул 0 компаний", elapsed)
    except Exception as exc:
        record("yandex.collect", False, str(exc)[:120], time.monotonic() - t0)


async def test_vk() -> None:
    print(head("VK — Playwright mobile scraper"))
    from src.enrichment.review_parsers.vk import VkParser

    # Публичная группа с отзывами (можно заменить на реальную страницу компании)
    test_url = "https://vk.com/wall_reviews"

    t0 = time.monotonic()
    try:
        parser = VkParser()
        # Просто проверяем, что can_handle работает и parse не крашится
        assert parser.can_handle("https://vk.com/some_company"), "can_handle failed"
        assert not parser.can_handle("https://flamp.ru/firm/x"), "can_handle false positive"

        reviews = await parser.parse(test_url, "Тест")
        elapsed = time.monotonic() - t0
        record(
            "vk.parse",
            True,
            f"вернул {len(reviews)} отзывов (0 — норма для публичной группы без стены)",
            elapsed,
        )
    except Exception as exc:
        record("vk.parse", False, str(exc)[:120], time.monotonic() - t0)


async def test_fssp() -> None:
    print(head("ФССП — httpx form submit + HTML parse"))
    from src.enrichment.registries.fssp import FsspChecker

    t0 = time.monotonic()
    try:
        checker = FsspChecker()
        result = await checker.check(name=FSSP_TEST_NAME)
        elapsed = time.monotonic() - t0

        if result.error and "captcha" not in (result.error or "").lower():
            record("fssp.check", False, f"error: {result.error}", elapsed)
        else:
            detail = (
                f"found={result.found}, status={result.status}"
                if not result.error
                else f"CAPTCHA → Playwright fallback (это нормально)"
            )
            record("fssp.check", True, detail, elapsed)
            if result.details:
                print(f"    Деталей: {result.details}")
    except Exception as exc:
        record("fssp.check", False, str(exc)[:120], time.monotonic() - t0)


async def test_flamp() -> None:
    print(head("Flamp — JSON API"))
    from src.enrichment.review_parsers.flamp import FlampParser

    parser = FlampParser()
    t0 = time.monotonic()
    try:
        assert parser.can_handle("https://omsk.flamp.ru/firm/test-12345")
        assert not parser.can_handle("https://2gis.ru/omsk/firms/123")
        # Попытка реального запроса (может вернуть 0 если filial_id несуществующий)
        reviews = await parser.parse("https://omsk.flamp.ru/firm/test-99999999", "Тест")
        elapsed = time.monotonic() - t0
        record("flamp.parse", True, f"вернул {len(reviews)} отзывов", elapsed)
    except Exception as exc:
        record("flamp.parse", False, str(exc)[:120], time.monotonic() - t0)


async def test_otzovik() -> None:
    print(head("Otzovik — HTML scraper"))
    from src.enrichment.review_parsers.otzovik import OtzovikParser

    parser = OtzovikParser()
    t0 = time.monotonic()
    try:
        assert parser.can_handle("https://otzovik.com/reviews/test/")
        assert not parser.can_handle("https://flamp.ru/firm/x")
        reviews = await parser.parse("https://otzovik.com/reviews/burenie-skvazhin_omsk/", "Тест")
        elapsed = time.monotonic() - t0
        record("otzovik.parse", True, f"вернул {len(reviews)} отзывов", elapsed)
    except Exception as exc:
        record("otzovik.parse", False, str(exc)[:120], time.monotonic() - t0)


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary() -> int:
    passed = sum(1 for r in results if r.success)
    total  = len(results)
    print(f"\n{BOLD}{'─'*55}{RESET}")
    print(f"{BOLD}Итого: {passed}/{total} прошли{RESET}")
    for r in results:
        icon = ok(r.name) if r.success else fail(r.name)
        print(f"  {icon}")
    if passed < total:
        print(f"\n{RED}Есть проблемы — смотри детали выше.{RESET}")
        return 1
    print(f"\n{GREEN}Все проверки прошли успешно!{RESET}")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

ALL_TESTS = {
    "twogis":  test_twogis,
    "avito":   test_avito,
    "yandex":  test_yandex,
    "vk":      test_vk,
    "fssp":    test_fssp,
    "flamp":   test_flamp,
    "otzovik": test_otzovik,
}


async def main(only: list[str] | None = None) -> int:
    selected = only or list(ALL_TESTS.keys())
    unknown  = [k for k in selected if k not in ALL_TESTS]
    if unknown:
        print(fail(f"Неизвестные тесты: {', '.join(unknown)}"))
        print(f"Доступные: {', '.join(ALL_TESTS)}")
        return 1

    print(f"{BOLD}Avito Parser — Smoke Test{RESET}")
    print(f"Тестируем: {', '.join(selected)}")
    print(f"Ключевое слово: «{TEST_KEYWORD}»")
    print(f"Прокси: {'настроен' if _proxy_configured() else 'не настроен (прямое подключение)'}")

    for name in selected:
        await ALL_TESTS[name]()

    return print_summary()


def _proxy_configured() -> bool:
    try:
        from src.proxy import proxy_manager
        return proxy_manager.count > 0
    except Exception:
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke-test коллекторов без БД")
    parser.add_argument(
        "--only",
        help="Запустить только указанные тесты через запятую: twogis,avito,fssp,...",
    )
    args = parser.parse_args()

    only_list = [x.strip() for x in args.only.split(",")] if args.only else None
    sys.exit(asyncio.run(main(only=only_list)))
