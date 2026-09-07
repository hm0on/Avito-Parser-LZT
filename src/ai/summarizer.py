"""OpenAI GPT-4o-mini review summarizer."""

from __future__ import annotations

import asyncio
import re

import httpx
import structlog
from openai import AsyncOpenAI, PermissionDeniedError, RateLimitError

from src.config import settings
from src.proxy import iter_proxy_urls, openai_proxy_manager

log = structlog.get_logger(__name__)

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
    def _cap_reviews(self, reviews: list[dict]) -> list[dict]:
        input_limit = max(1, int(settings.summarizer_max_input_reviews))
        factual_limit = max(1, int(settings.max_reviews_per_company))
        return reviews[: min(input_limit, factual_limit)]

    @staticmethod
    def _extract_retry_after_seconds(exc: Exception) -> float | None:
        text = str(exc)
        match = re.search(r"Please try again in ([0-9]+(?:\.[0-9]+)?)ms", text)
        if match:
            return max(0.5, float(match.group(1)) / 1000.0)

        match = re.search(r"Please try again in ([0-9]+(?:\.[0-9]+)?)s", text)
        if match:
            return max(0.5, float(match.group(1)))

        return None

    async def _request_summary(self, prompt: str, *, proxy_url: str | None) -> str:
        async with httpx.AsyncClient(proxy=(proxy_url or None), timeout=120.0) as http_client:
            client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=http_client)
            response = await client.chat.completions.create(
                model=settings.summarizer_model,
                max_tokens=max(500, settings.summarizer_max_tokens),
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=settings.summarizer_temperature,
            )

        summary = response.choices[0].message.content or ""
        log.info("summarizer.done", tokens=response.usage.total_tokens, proxy=proxy_url or "direct")
        return summary.strip()

    def _iter_proxy_candidates(self):
        if openai_proxy_manager.count <= 0:
            yield None
            return

        for proxy_url in iter_proxy_urls(openai_proxy_manager, purpose="openai summarizer"):
            yield proxy_url

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

        capped = self._cap_reviews(reviews)

        per_review_limit = 1000 if settings.openai_quality_profile else 500

        # Build reviews text block.
        lines = []
        for i, r in enumerate(capped, 1):
            text = (r.get("text") or "").strip()[:per_review_limit]
            rating = r.get("rating", "?")
            date = r.get("review_date", "")
            source = r.get("source", "")
            lines.append(f"[{i}] Оценка: {rating}/5 ({date}) [{source}]\n{text}")

        reviews_text = "\n\n".join(lines)
        prompt = _USER_TEMPLATE.format(
            name=company_name,
            count=len(capped),
            reviews_text=reviews_text,
        )

        log.info("summarizer.start", company=company_name, review_count=len(capped))
        attempts = 0
        for attempts, proxy_url in enumerate(self._iter_proxy_candidates(), start=1):
            try:
                return await self._request_summary(prompt, proxy_url=proxy_url)
            except PermissionDeniedError as exc:
                error_text = str(exc)
                if "unsupported_country_region_territory" in error_text:
                    log.warning(
                        "summarizer.proxy_unsupported_region",
                        company=company_name,
                        attempt=attempts,
                        proxy=proxy_url or "direct",
                    )
                    continue
                log.warning(
                    "summarizer.attempt_failed",
                    company=company_name,
                    attempt=attempts,
                    proxy=proxy_url or "direct",
                    error=error_text,
                )
            except RateLimitError as exc:
                retry_after_s = min(45.0, self._extract_retry_after_seconds(exc) or 5.0)
                log.warning(
                    "summarizer.rate_limited",
                    company=company_name,
                    attempt=attempts,
                    proxy=proxy_url or "direct",
                    retry_after_s=retry_after_s,
                    error=str(exc),
                )
                await asyncio.sleep(retry_after_s)
            except Exception as exc:
                log.warning(
                    "summarizer.attempt_failed",
                    company=company_name,
                    attempt=attempts,
                    proxy=proxy_url or "direct",
                    error=str(exc),
                )

        log.error("summarizer.error", company=company_name, attempts=attempts, error="all attempts failed")
        return None
