from __future__ import annotations

from src.ai.summarizer import ReviewSummarizer


def test_cap_reviews_respects_summarizer_limit(monkeypatch):
    monkeypatch.setattr("src.ai.summarizer.settings.summarizer_max_input_reviews", 3)
    monkeypatch.setattr("src.ai.summarizer.settings.max_reviews_per_company", 10)
    summarizer = ReviewSummarizer()

    capped = summarizer._cap_reviews([{"text": str(i)} for i in range(6)])

    assert len(capped) == 3


def test_extract_retry_after_seconds_supports_ms_and_s():
    summarizer = ReviewSummarizer()

    ms_value = summarizer._extract_retry_after_seconds(Exception("Please try again in 522ms"))
    sec_value = summarizer._extract_retry_after_seconds(Exception("Please try again in 42.41s"))

    assert ms_value == 0.522
    assert sec_value == 42.41
