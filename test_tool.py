"""
Универсальный тестовый инструмент — проверка компонентов пайплайна без БД.

Примеры:
    python test_tool.py yandex                          # Яндекс.Карты, дефолтный keyword
    python test_tool.py twogis -k "ремонт квартир"      # 2GIS, свой keyword
    python test_tool.py avito                            # Авито
    python test_tool.py inn 5503098042                   # DaData по ИНН
    python test_tool.py inn 1025500736647                # DaData по ОГРН
    python test_tool.py website https://example.ru       # Сканирование сайта
    python test_tool.py fssp --inn 5503098042            # ФССП по ИНН
    python test_tool.py fssp --name "ООО Ромашка"        # ФССП по имени
    python test_tool.py registries 5503098042              # Все 13 реестров по ИНН
    python test_tool.py enrich twogis -k "монтаж"        # Collect 1 + enrich
    python test_tool.py all                              # Все тесты разом
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

DEFAULT_KEYWORD = "бурение скважин"


def ok(msg: str)   -> str: return f"{GREEN}✓{RESET} {msg}"
def warn(msg: str) -> str: return f"{YELLOW}⚠{RESET} {msg}"
def fail(msg: str) -> str: return f"{RED}✗{RESET} {msg}"
def head(msg: str) -> str: return f"\n{BOLD}{'─'*60}\n  {msg}\n{'─'*60}{RESET}"
def dim(msg: str)  -> str: return f"{DIM}{msg}{RESET}"
def cyan(msg: str) -> str: return f"{CYAN}{msg}{RESET}"


def _print_company(c, idx: int = 0) -> None:
    """Print RawCompany details."""
    print(f"  {BOLD}[{idx+1}] {c.name_raw or '—'}{RESET}")
    print(f"      Источник:  {c.source}")
    if c.source_link:
        print(f"      Ссылка:    {c.source_link}")
    if c.phones:
        print(f"      Телефоны:  {', '.join(c.phones)}")
    if c.emails:
        print(f"      Email:     {', '.join(c.emails)}")
    if c.addresses:
        print(f"      Адреса:    {'; '.join(c.addresses)}")
    if c.inn:
        print(f"      ИНН:       {c.inn}")
    if c.ogrn:
        print(f"      ОГРН:      {c.ogrn}")
    if c.average_rating:
        print(f"      Рейтинг:   {c.average_rating}  ({c.reviews_count or 0} отзывов)")
    if c.reviews:
        print(f"      Отзывов:   {len(c.reviews)} текстов")
        for r in c.reviews[:2]:
            text_preview = (r.text[:80] + "…") if r.text and len(r.text) > 80 else (r.text or "—")
            print(f"        - [{r.rating or '?'}] {text_preview}")


def _print_check_result(result) -> None:
    """Print CheckResult details."""
    print(f"  Реестр:  {result.registry}")
    print(f"  Найден:  {result.found}")
    if result.status:
        print(f"  Статус:  {result.status}")
    if result.error:
        print(f"  {RED}Ошибка:  {result.error}{RESET}")
    if result.details:
        print(f"  Детали:")
        for k, v in result.details.items():
            print(f"    {k}: {v}")


# ── Collector commands ───────────────────────────────────────────────────────

async def cmd_collector(source: str, keyword: str) -> bool:
    """Run a collector and display results."""
    collectors = {
        "yandex": ("src.collectors.yandex", "YandexCollector", "Яндекс.Карты"),
        "twogis": ("src.collectors.twogis", "TwoGisCollector", "2GIS"),
        "avito":  ("src.collectors.avito", "AvitoCollector", "Авито"),
    }

    mod_path, cls_name, label = collectors[source]
    print(head(f"{label} — collect('{keyword}')"))

    t0 = time.monotonic()
    try:
        import importlib
        mod = importlib.import_module(mod_path)
        CollectorClass = getattr(mod, cls_name)
        collector = CollectorClass()
        companies = await collector.collect(keyword)
        elapsed = time.monotonic() - t0

        if not companies:
            print(f"  {fail('0 компаний получено')}")
            print(f"  {dim(f'Время: {elapsed:.1f}s')}")
            if source == "avito":
                print(f"  {warn('Авито блокирует запросы без прокси (429). Нужен резидентный прокси или spfa.ru cookies.')}")
            return False

        print(f"  {ok(f'{len(companies)} компаний за {elapsed:.1f}s')}")
        print()

        # Show first 5
        for i, c in enumerate(companies[:5]):
            _print_company(c, i)
            print()

        if len(companies) > 5:
            print(f"  {dim(f'... и ещё {len(companies) - 5}')}")

        # Stats
        with_phone   = sum(1 for c in companies if c.phones)
        with_addr    = sum(1 for c in companies if c.addresses)
        with_rating  = sum(1 for c in companies if c.average_rating)
        with_reviews = sum(1 for c in companies if c.reviews)
        with_inn     = sum(1 for c in companies if c.inn)
        print(f"\n  {BOLD}Статистика:{RESET}")
        print(f"    Телефоны:  {with_phone}/{len(companies)}")
        print(f"    Адреса:    {with_addr}/{len(companies)}")
        print(f"    Рейтинг:   {with_rating}/{len(companies)}")
        print(f"    Отзывы:    {with_reviews}/{len(companies)}")
        print(f"    ИНН:       {with_inn}/{len(companies)}")
        return True

    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  {fail(str(exc))}")
        print(f"  {dim(f'Время: {elapsed:.1f}s')}")
        import traceback
        traceback.print_exc()
        return False


# ── DaData (INN/OGRN lookup) ────────────────────────────────────────────────

async def cmd_inn(query: str) -> bool:
    """Look up a company by INN or OGRN via DaData."""
    print(head(f"DaData — поиск «{query}»"))

    t0 = time.monotonic()
    try:
        from src.enrichment.registries.dadata import DaDataChecker
        checker = DaDataChecker()

        # Determine if INN or OGRN by length
        kwargs = {}
        if len(query) in (10, 12):
            kwargs["inn"] = query
            print(f"  Тип запроса: ИНН ({len(query)} цифр)")
        elif len(query) in (13, 15):
            kwargs["ogrn"] = query
            print(f"  Тип запроса: ОГРН ({len(query)} цифр)")
        else:
            # Try as name
            kwargs["name"] = query
            print(f"  Тип запроса: поиск по имени")

        result = await checker.safe_check(**kwargs)
        elapsed = time.monotonic() - t0

        print(f"  {dim(f'Время: {elapsed:.1f}s')}")
        print()
        _print_check_result(result)
        if result.error:
            print(f"\n  {warn('Подсказка: проверьте DADATA_API_KEY/DADATA_SECRET_KEY в .env и доступ к suggestions.dadata.ru')}")
        return result.found

    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  {fail(str(exc))}")
        print(f"  {dim(f'Время: {elapsed:.1f}s')}")
        import traceback
        traceback.print_exc()
        return False


# ── Website scanner ──────────────────────────────────────────────────────────

async def cmd_website(url: str) -> bool:
    """Scan a website for INN/OGRN/contacts."""
    print(head(f"WebsiteScanner — {url}"))

    t0 = time.monotonic()
    try:
        from src.enrichment.website_scanner import WebsiteScanner
        scanner = WebsiteScanner()
        result = await scanner.scan(url)
        elapsed = time.monotonic() - t0

        print(f"  {dim(f'Время: {elapsed:.1f}s')}")
        print()
        print(f"  Сайт:         {result.website}")
        print(f"  Страниц:      {result.pages_scanned}")
        print(f"  ИНН:          {result.inn or '—'}  {'✓' if result.inn_found else '✗'}")
        print(f"  ОГРН:         {result.ogrn or '—'}  {'✓' if result.ogrn_found else '✗'}")
        if result.phones:
            print(f"  Телефоны:     {', '.join(result.phones)}")
        if result.emails:
            print(f"  Email:        {', '.join(result.emails)}")
        if result.notes:
            print(f"  Заметки:")
            for n in result.notes:
                print(f"    - {n}")

        found_something = result.inn_found or result.ogrn_found or result.phones or result.emails
        if found_something:
            print(f"\n  {ok('Данные найдены')}")
        else:
            print(f"\n  {warn('Ничего не найдено')}")
        return found_something

    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  {fail(str(exc))}")
        import traceback
        traceback.print_exc()
        return False


# ── FSSP ─────────────────────────────────────────────────────────────────────

async def cmd_fssp(inn: str | None = None, name: str | None = None) -> bool:
    """Check FSSP for enforcement proceedings."""
    query_desc = f"ИНН {inn}" if inn else f"имя «{name}»"
    print(head(f"ФССП — {query_desc}"))

    if not inn and not name:
        print(f"  {fail('Укажите --inn или --name')}")
        return False

    t0 = time.monotonic()
    try:
        from src.enrichment.registries.fssp import FsspChecker
        checker = FsspChecker()
        result = await checker.safe_check(inn=inn, name=name)
        elapsed = time.monotonic() - t0

        print(f"  {dim(f'Время: {elapsed:.1f}s')}")
        print()
        _print_check_result(result)

        if result.error:
            err_lower = result.error.lower()
            if "captcha" in err_lower:
                print(f"\n  {warn('CAPTCHA — нужен прокси или ручное решение')}")
            elif "could not parse" in err_lower:
                print(f"\n  {warn('ФССП ставит CAPTCHA на GET-запрос. httpx не может обойти, Playwright тоже не смог.')}")
                print(f"  {warn('Решение: использовать резидентный прокси или CAPTCHA-solver.')}")
            return False

        return True

    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  {fail(str(exc))}")
        import traceback
        traceback.print_exc()
        return False


# ── Enrich (collect 1 company → full enrichment) ────────────────────────────

async def cmd_enrich(source: str, keyword: str) -> bool:
    """Collect companies from a source, then enrich the first one."""
    print(head(f"Enrich — {source} → collect → enrich"))
    print(f"  Keyword: «{keyword}»")

    t0 = time.monotonic()
    try:
        # Step 1: Collect
        import importlib
        collectors = {
            "yandex": ("src.collectors.yandex", "YandexCollector"),
            "twogis": ("src.collectors.twogis", "TwoGisCollector"),
            "avito":  ("src.collectors.avito", "AvitoCollector"),
        }
        if source not in collectors:
            print(f"  {fail(f'Неизвестный источник: {source}. Доступные: {', '.join(collectors)}')}")
            return False

        mod_path, cls_name = collectors[source]
        mod = importlib.import_module(mod_path)
        CollectorClass = getattr(mod, cls_name)

        print(f"\n  {cyan('Шаг 1:')} Сбор компаний...")
        collector = CollectorClass()
        companies = await collector.collect(keyword)

        if not companies:
            print(f"  {fail('0 компаний — нечего обогащать')}")
            return False

        print(f"  {ok(f'{len(companies)} компаний собрано')}")

        # Pick the first company with most data
        company = companies[0]
        _print_company(company)

        # Step 2: Convert RawCompany → CompanyRaw (SQLAlchemy model, in-memory)
        print(f"\n  {cyan('Шаг 2:')} Обогащение (enrichment)...")
        from src.database.models import CompanyRaw
        raw_db = CompanyRaw(
            id=uuid.uuid4(),
            source=company.source,
            source_id=company.source_id,
            source_link=company.source_link,
            name_raw=company.name_raw,
            phones=company.phones,
            emails=company.emails,
            addresses=company.addresses,
            contacts_json=company.contacts_json or {},
            inn=company.inn,
            ogrn=company.ogrn,
            average_rating=company.average_rating,
            reviews_count=company.reviews_count,
        )

        from src.enrichment.enricher import Enricher
        enricher = Enricher()
        enriched, extra_reviews = await enricher.enrich(raw_db)
        elapsed = time.monotonic() - t0

        print(f"  {ok(f'Обогащение завершено за {elapsed:.1f}s')}")
        print()
        print(f"  {BOLD}Результат:{RESET}")
        print(f"    Имя (норм.):  {enriched.name_normalized}")
        print(f"    Тип:           {enriched.entity_type}")
        print(f"    ИНН:           {enriched.inn or '—'}")
        print(f"    ОГРН:          {enriched.ogrn or '—'}")
        print(f"    Телефоны:      {enriched.phones_normalized or '—'}")
        print(f"    Адреса:        {json.dumps(enriched.addresses_parsed, ensure_ascii=False, default=str) if enriched.addresses_parsed else '—'}")
        print(f"    Confidence:    {enriched.confidence_score}/100")
        print(f"    Ручная пров.:  {'да' if enriched.manual_review_required else 'нет'}")

        if enriched.checks:
            print(f"\n    {BOLD}Проверки реестров:{RESET}")
            for reg_name, reg_data in enriched.checks.items():
                status_icon = ok(reg_name) if reg_data.get("found") else dim(f"✗ {reg_name}")
                print(f"      {status_icon}: {reg_data.get('status', '—')}")

        if extra_reviews:
            print(f"\n    {BOLD}Доп. отзывы:{RESET} {len(extra_reviews)}")
            for r in extra_reviews[:3]:
                text_preview = (r.text[:80] + "…") if r.text and len(r.text) > 80 else (r.text or "—")
                print(f"      [{r.source}] [{r.rating or '?'}] {text_preview}")

        return True

    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  {fail(str(exc))}")
        print(f"  {dim(f'Время: {elapsed:.1f}s')}")
        import traceback
        traceback.print_exc()
        return False


# ── All registries check by INN ───────────────────────────────────────────────

async def cmd_registries(query: str) -> bool:
    """Run ALL 13 registry checkers on a given INN/OGRN."""
    print(head(f"Все реестры — проверка «{query}»"))

    # Determine INN vs OGRN
    inn, ogrn = None, None
    if len(query) in (10, 12):
        inn = query
        print(f"  Тип: ИНН ({len(query)} цифр)")
    elif len(query) in (13, 15):
        ogrn = query
        print(f"  Тип: ОГРН ({len(query)} цифр)")
    else:
        inn = query
        print(f"  Тип: неизвестный, пробуем как ИНН")

    t0 = time.monotonic()
    try:
        from src.enrichment.enricher import _PASS1_CHECKERS, _PASS2_CHECKERS
        import asyncio as _aio

        # Pass 1
        print(f"\n  {cyan('Pass 1:')} {len(_PASS1_CHECKERS)} чекеров...")
        results = await _aio.gather(
            *[c.safe_check(inn=inn, ogrn=ogrn) for c in _PASS1_CHECKERS]
        )

        checks = {}
        for r in results:
            checks[r.registry] = r

        # Show pass 1 results
        for r in results:
            if r.error:
                print(f"    {fail(r.registry)}: {r.error}")
            elif r.found:
                print(f"    {ok(r.registry)}: {r.status or 'found'}")
            else:
                print(f"    {dim(f'— {r.registry}: не найдено')}")

        # Extract data for pass 2
        director_fio = ""
        legal_address = ""

        pb = checks.get("fns_pb")
        if pb and pb.found:
            director_fio = pb.details.get("head_fio", "")
            legal_address = pb.details.get("address", "")

        dd = checks.get("dadata_fns")
        if dd and dd.found:
            if not director_fio and dd.details.get("entity_type") == "ИП":
                director_fio = dd.details.get("name", "")
            if not legal_address:
                legal_address = dd.details.get("address", "")

        # Pass 2
        if director_fio or legal_address:
            print(f"\n  {cyan('Pass 2:')} {len(_PASS2_CHECKERS)} чекеров...")
            if director_fio:
                print(f"    Директор: {director_fio}")
            if legal_address:
                print(f"    Адрес: {legal_address[:60]}...")

            extra = {"director_fio": director_fio, "legal_address": legal_address}
            pass2_results = await _aio.gather(
                *[c.safe_check(inn=inn, ogrn=ogrn, extra=extra) for c in _PASS2_CHECKERS]
            )
            for r in pass2_results:
                checks[r.registry] = r
                if r.error:
                    print(f"    {fail(r.registry)}: {r.error}")
                elif r.found:
                    print(f"    {ok(r.registry)}: {r.status or 'found'}")
                else:
                    print(f"    {dim(f'— {r.registry}: не найдено')}")
        else:
            print(f"\n  {dim('Pass 2 пропущен: нет данных о директоре/адресе из pass 1')}")

        elapsed = time.monotonic() - t0

        # Summary with details
        print(f"\n  {BOLD}Итого: {len(checks)} реестров за {elapsed:.1f}s{RESET}")
        found_count = sum(1 for r in checks.values() if r.found)
        error_count = sum(1 for r in checks.values() if r.error)
        print(f"    Найдено: {found_count}, Ошибки: {error_count}")

        # Show detailed results for found items
        for name, r in checks.items():
            if r.found and r.details:
                print(f"\n    {BOLD}{name}{RESET} ({r.status}):")
                for k, v in r.details.items():
                    if isinstance(v, list) and len(v) > 3:
                        print(f"      {k}: [{len(v)} items]")
                    elif isinstance(v, str) and len(v) > 80:
                        print(f"      {k}: {v[:80]}…")
                    else:
                        print(f"      {k}: {v}")

        return found_count > 0

    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  {fail(str(exc))}")
        print(f"  {dim(f'Время: {elapsed:.1f}s')}")
        import traceback
        traceback.print_exc()
        return False


# ── All tests ────────────────────────────────────────────────────────────────

async def cmd_all(keyword: str) -> None:
    """Run all smoke tests sequentially."""
    print(f"{BOLD}Avito Parser — Полный тест{RESET}")
    print(f"Ключевое слово: «{keyword}»")
    print()

    results: list[tuple[str, bool]] = []

    # Collectors
    for source in ("twogis", "yandex", "avito"):
        success = await cmd_collector(source, keyword)
        results.append((source, success))

    # DaData
    success = await cmd_inn("5503098042")
    results.append(("dadata/inn", success))

    # All registries by INN
    success = await cmd_registries("5503098042")
    results.append(("registries", success))

    # FSSP
    success = await cmd_fssp(name="Рога и Копыта")
    results.append(("fssp", success))

    # Summary
    print(head("Итого"))
    passed = sum(1 for _, s in results if s)
    print(f"  {BOLD}{passed}/{len(results)} прошли{RESET}")
    for name, success in results:
        icon = ok(name) if success else fail(name)
        print(f"    {icon}")

    if passed < len(results):
        print(f"\n  {RED}Есть проблемы — смотри детали выше.{RESET}")
    else:
        print(f"\n  {GREEN}Все проверки прошли!{RESET}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Avito Parser — тестовый инструмент для проверки компонентов",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Примеры:
  python test_tool.py twogis                        # 2GIS, дефолтный keyword
  python test_tool.py yandex -k "ремонт квартир"    # Яндекс, свой keyword
  python test_tool.py inn 5503098042                 # DaData по ИНН
  python test_tool.py website https://example.ru     # Сканирование сайта
  python test_tool.py fssp --inn 5503098042          # ФССП по ИНН
  python test_tool.py registries 5503098042          # Все 13 реестров по ИНН
  python test_tool.py enrich twogis                  # Collect + enrich
  python test_tool.py all                            # Все тесты
""",
    )
    sub = parser.add_subparsers(dest="command", help="Команда для запуска")

    # Collectors
    for name in ("yandex", "twogis", "avito"):
        p = sub.add_parser(name, help=f"Тест коллектора {name}")
        p.add_argument("-k", "--keyword", default=DEFAULT_KEYWORD, help="Ключевое слово для поиска")

    # DaData INN/OGRN lookup
    p_inn = sub.add_parser("inn", help="Поиск по ИНН/ОГРН через DaData")
    p_inn.add_argument("query", help="ИНН (10/12 цифр) или ОГРН (13/15 цифр)")

    # Website scanner
    p_web = sub.add_parser("website", help="Сканирование сайта на ИНН/ОГРН/контакты")
    p_web.add_argument("url", help="URL сайта для сканирования")

    # FSSP
    p_fssp = sub.add_parser("fssp", help="Проверка ФССП")
    p_fssp.add_argument("--inn", default=None, help="ИНН для проверки")
    p_fssp.add_argument("--name", default=None, help="Название организации")

    # Enrich (collect + enrich)
    p_enrich = sub.add_parser("enrich", help="Collect + enrich одной компании")
    p_enrich.add_argument("source", choices=["yandex", "twogis", "avito"], help="Источник для сбора")
    p_enrich.add_argument("-k", "--keyword", default=DEFAULT_KEYWORD, help="Ключевое слово для поиска")

    # Registries (all 13 checkers by INN)
    p_reg = sub.add_parser("registries", help="Проверка всех 13 реестров по ИНН/ОГРН")
    p_reg.add_argument("query", help="ИНН (10/12 цифр) или ОГРН (13/15 цифр)")

    # All
    p_all = sub.add_parser("all", help="Запуск всех тестов")
    p_all.add_argument("-k", "--keyword", default=DEFAULT_KEYWORD, help="Ключевое слово для поиска")

    return parser


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    cmd = args.command

    if cmd in ("yandex", "twogis", "avito"):
        success = await cmd_collector(cmd, args.keyword)
        return 0 if success else 1

    if cmd == "inn":
        success = await cmd_inn(args.query)
        return 0 if success else 1

    if cmd == "website":
        success = await cmd_website(args.url)
        return 0 if success else 1

    if cmd == "fssp":
        success = await cmd_fssp(inn=args.inn, name=args.name)
        return 0 if success else 1

    if cmd == "enrich":
        success = await cmd_enrich(args.source, args.keyword)
        return 0 if success else 1

    if cmd == "registries":
        success = await cmd_registries(args.query)
        return 0 if success else 1

    if cmd == "all":
        await cmd_all(args.keyword)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
