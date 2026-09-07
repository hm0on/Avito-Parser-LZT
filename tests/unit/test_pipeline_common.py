from datetime import date
from types import SimpleNamespace

import pytest

from src.pipeline.common import required_sources_completed


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):  # noqa: ARG002
        return _Result(self._rows)


def _checkpoint(source: str, status: str, companies_count: int):
    return SimpleNamespace(
        source=source,
        status=status,
        companies_count=companies_count,
    )


@pytest.mark.asyncio
async def test_required_sources_completed_accepts_all_non_empty_completed():
    rows = [
        _checkpoint("avito", "completed", 10),
        _checkpoint("yandex", "completed", 8),
        _checkpoint("2gis", "completed", 6),
    ]
    session = _Session(rows)

    assert await required_sources_completed(session, week_start=date(2026, 2, 23)) is True


@pytest.mark.asyncio
async def test_required_sources_completed_rejects_completed_with_zero_companies():
    rows = [
        _checkpoint("avito", "completed", 10),
        _checkpoint("yandex", "completed", 8),
        _checkpoint("2gis", "completed", 0),
    ]
    session = _Session(rows)

    assert await required_sources_completed(session, week_start=date(2026, 2, 23)) is False


@pytest.mark.asyncio
async def test_required_sources_completed_rejects_missing_or_non_completed_sources():
    rows = [
        _checkpoint("avito", "completed", 10),
        _checkpoint("yandex", "running", 8),
    ]
    session = _Session(rows)

    assert await required_sources_completed(session, week_start=date(2026, 2, 23)) is False
