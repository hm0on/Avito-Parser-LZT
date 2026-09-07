"""Phone and address normalization utilities."""

import re

import phonenumbers
import structlog

log = structlog.get_logger(__name__)

_RU_DEFAULT_REGION = "RU"


def normalize_phone(raw: str) -> str | None:
    """Normalize a raw phone string to E.164 format (+7XXXXXXXXXX).

    Returns None if the number cannot be parsed.
    """
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, _RU_DEFAULT_REGION)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.phonenumberutil.NumberParseException:
        pass
    return None


def normalize_phones(phones: list[str]) -> list[str]:
    """Normalize a list of raw phone strings, deduplicating by E.164."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in phones:
        normalized = normalize_phone(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def parse_address(raw: str) -> dict:
    """Parse a raw address string into structured components.

    Returns a dict with keys: region, city, district, street, house, raw.
    This is a lightweight heuristic parser — use an external geocoder for
    production-grade accuracy.

    Handles Avito location format: "р-н Кировский· выезжает по городу"
    """
    if not raw:
        return {"raw": raw}

    # Avito uses "·" to separate district from coverage area
    # e.g. "р-н Кировский· выезжает по городу" → district + coverage
    clean = raw.split("·")[0].strip()

    parts = [p.strip() for p in clean.split(",") if p.strip()]
    result: dict = {"raw": raw}

    for part in parts:
        low = part.lower()
        # Check district BEFORE city — "р-н" contains priority
        if any(kw in low for kw in ("р-н", "район")):
            result.setdefault("district", _strip_prefix(part, ("р-н", "район")))
        elif any(kw in low for kw in ("обл.", "область", "край", "республика")):
            result.setdefault("region", part)
        elif any(kw in low for kw in ("г.", "город", "омск")):
            result.setdefault("city", _strip_prefix(part, ("г.", "город")))
        elif any(
            kw in low
            for kw in ("ул.", "пр.", "пр-т", "переулок", "бульвар", "шоссе", "площадь")
        ):
            result.setdefault("street", part)
        elif re.match(r"^\d", part):
            result.setdefault("house", part)

    return result


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> str:
    for pref in prefixes:
        if text.lower().startswith(pref.lower()):
            return text[len(pref):].strip()
    return text.strip()
