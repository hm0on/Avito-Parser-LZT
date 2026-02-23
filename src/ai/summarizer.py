"""OpenAI GPT-4o-mini review summarizer."""

from __future__ import annotations

import httpx
import structlog
from openai import AsyncOpenAI

from src.ai.sx_proxy import get_us_proxy
from src.config import settings

log = structlog.get_logger(__name__)

_MODEL = "gpt-4o-mini"
_MAX_TOKENS = 700

_SYSTEM_PROMPT = (
    "Ты аналитик отзывов о строительных и инженерных компаниях. "
    "Твоя задача — написать краткий, объективный анализ отзывов на русском языке."
)

_USER_TEMPLATE = """
Компания: {name}
Количество отзывов: {count}

Отзывы:
{reviews_text}

Напиши структурированное резюме (200–400 слов):
1. Краткое резюме (1 абзац).
2. 3 сильных стороны (маркированный список).
3. 3 слабых стороны (маркированный список).
4. Динамика за последние 12 месяцев (1–2 предложения).
"""


class ReviewSummarizer:
    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None
        self._http_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            # Get sx.org US proxy for OpenAI (bypasses Russian blocks)
            proxy_url = await get_us_proxy()
            if proxy_url:
                log.info("summarizer.using_proxy", proxy=proxy_url.split("@")[-1])
                self._http_client = httpx.AsyncClient(proxy=proxy_url)
                self._client = AsyncOpenAI(
                    api_key=settings.openai_api_key,
                    http_client=self._http_client,
                )
            else:
                log.info("summarizer.direct_connection")
                self._client = AsyncOpenAI(api_key=settings.openai_api_key)
        return self._client

    async def summarize(
        self,
        company_name: str,
        reviews: list[dict],
    ) -> str | None:
        """Generate a structured summary of reviews.

        Args:
            company_name: Human-readable company name.
            reviews: List of review dicts with keys: text, rating, review_date.

        Returns:
            Summary text or None if summarization fails/is skipped.
        """
        if not settings.openai_api_key:
            log.warning("summarizer.skip", reason="OPENAI_API_KEY not set")
            return None

        if not reviews:
            return None

        # Limit to MAX_REVIEWS_PER_COMPANY
        capped = reviews[: settings.max_reviews_per_company]

        # Build reviews text block (truncate each to 500 chars for token efficiency)
        lines = []
        for i, r in enumerate(capped, 1):
            text = (r.get("text") or "").strip()[:500]
            rating = r.get("rating", "?")
            date = r.get("review_date", "")
            lines.append(f"[{i}] Оценка: {rating}/5 ({date})\n{text}")

        reviews_text = "\n\n".join(lines)
        prompt = _USER_TEMPLATE.format(
            name=company_name,
            count=len(capped),
            reviews_text=reviews_text,
        )

        log.info("summarizer.start", company=company_name, review_count=len(capped))
        try:
            client = await self._get_client()
            response = await client.chat.completions.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            summary = response.choices[0].message.content or ""
            log.info("summarizer.done", company=company_name, tokens=response.usage.total_tokens)
            return summary.strip()
        except Exception as exc:
            log.error("summarizer.error", company=company_name, error=str(exc))
            return None
