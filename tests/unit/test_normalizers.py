"""Unit tests for phone and address normalizers."""

import pytest

from src.enrichment.normalizers import normalize_phone, normalize_phones, parse_address


# ── Phone normalization ─────────────────────────────────────────────────────


class TestNormalizePhone:
    def test_e164_already_formatted(self):
        assert normalize_phone("+79131234567") == "+79131234567"

    def test_russian_format_with_parentheses(self):
        assert normalize_phone("+7 (913) 123-45-67") == "+79131234567"

    def test_eight_prefix(self):
        assert normalize_phone("8-913-123-45-67") == "+79131234567"

    def test_eight_prefix_compact(self):
        assert normalize_phone("89131234567") == "+79131234567"

    def test_seven_prefix_compact(self):
        assert normalize_phone("79131234567") == "+79131234567"

    def test_invalid_too_short(self):
        assert normalize_phone("123") is None

    def test_invalid_letters(self):
        assert normalize_phone("не телефон") is None

    def test_empty_string(self):
        assert normalize_phone("") is None

    def test_none_like_empty(self):
        # normalize_phone accepts str, empty str → None
        assert normalize_phone("   ") is None


class TestNormalizePhones:
    def test_deduplicates_same_number_different_format(self):
        phones = ["+7 (913) 123-45-67", "89131234567", "+79131234567"]
        result = normalize_phones(phones)
        assert result == ["+79131234567"]

    def test_preserves_multiple_distinct_numbers(self):
        phones = ["+79131234567", "+79139876543"]
        result = normalize_phones(phones)
        assert len(result) == 2
        assert "+79131234567" in result
        assert "+79139876543" in result

    def test_filters_invalid(self):
        phones = ["invalid", "+79131234567", "garbage"]
        result = normalize_phones(phones)
        assert result == ["+79131234567"]

    def test_empty_list(self):
        assert normalize_phones([]) == []


# ── Address parsing ─────────────────────────────────────────────────────────


class TestParseAddress:
    def test_full_address(self):
        raw = "г. Омск, ул. Ленина, 10"
        result = parse_address(raw)
        assert result["raw"] == raw
        assert "city" in result
        assert "street" in result
        assert "house" in result

    def test_city_extracted(self):
        result = parse_address("г. Омск, ул. Тарская, 5")
        assert "Омск" in result.get("city", "")

    def test_region_extracted(self):
        result = parse_address("Омская обл., г. Омск, пр. Мира, 1")
        assert "region" in result

    def test_district_extracted(self):
        result = parse_address("г. Омск, Ленинский р-н, ул. Тарская, 12")
        assert "district" in result

    def test_empty_string(self):
        result = parse_address("")
        assert result == {"raw": ""}

    def test_raw_always_preserved(self):
        raw = "Омск, пр. Маркса, 37"
        result = parse_address(raw)
        assert result["raw"] == raw
