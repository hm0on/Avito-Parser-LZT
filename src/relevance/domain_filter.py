"""Rule-based plumbing-domain relevance checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_HARD_ALLOW_TOKENS = (
    "сантех",
    "отоплен",
    "водоснаб",
    "водопровод",
    "канализац",
    "радиатор",
    "котел",
    "бойлер",
    "трубы",
    "санузел",
    "сварк",
    "септик",
    "водонагрев",
    "протечк",
)

_HARD_DENY_TOKENS = (
    "детский сад",
    "школа",
    "пекар",
    "кафе",
    "ресторан",
    "салон красоты",
    "парикмахер",
    "зоомагазин",
    "аптека",
    "медицинск",
    "стоматолог",
    "автосервис",
    "шиномонтаж",
)

_NON_WORD = re.compile(r"\s+")


@dataclass(slots=True)
class DomainDecision:
    score: float
    domain_pass: bool
    allow_hits: list[str] = field(default_factory=list)
    deny_hits: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    ambiguous: bool = False


def _contains_token(text: str, token: str) -> bool:
    if " " in token:
        return token in text
    return bool(re.search(rf"\b{re.escape(token)}[а-яa-z0-9\-]*", text))


def evaluate_domain(*, name: str | None, keyword: str | None, snippet: str | None) -> DomainDecision:
    text = " ".join(
        part.strip().lower()
        for part in (name or "", keyword or "", snippet or "")
        if part and part.strip()
    )
    text = _NON_WORD.sub(" ", text).strip()

    allow_hits = [token for token in _HARD_ALLOW_TOKENS if _contains_token(text, token)]
    deny_hits = [token for token in _HARD_DENY_TOKENS if _contains_token(text, token)]

    if allow_hits and not deny_hits:
        score = min(0.98, 0.78 + 0.05 * len(allow_hits))
        return DomainDecision(
            score=score,
            domain_pass=True,
            allow_hits=allow_hits,
            deny_hits=[],
            reason_codes=["domain_allow_tokens"],
            ambiguous=False,
        )

    if deny_hits and not allow_hits:
        return DomainDecision(
            score=0.05,
            domain_pass=False,
            allow_hits=[],
            deny_hits=deny_hits,
            reason_codes=["domain_deny_tokens"],
            ambiguous=False,
        )

    if allow_hits and deny_hits:
        return DomainDecision(
            score=0.45,
            domain_pass=False,
            allow_hits=allow_hits,
            deny_hits=deny_hits,
            reason_codes=["domain_conflict_tokens"],
            ambiguous=True,
        )

    return DomainDecision(
        score=0.3,
        domain_pass=False,
        allow_hits=[],
        deny_hits=[],
        reason_codes=["domain_no_signal"],
        ambiguous=True,
    )
