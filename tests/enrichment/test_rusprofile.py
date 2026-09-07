"""Unit tests for rusprofile fallback checker helpers."""

from src.enrichment.registries.base import CheckResult
from src.enrichment.registries.rusprofile import (
    _extract_search_candidates,
    _parse_company_page,
    _select_best_candidate,
    _is_zero_results,
    map_rusprofile_to_registries,
)

_SEARCH_HTML = """
<html><body>
  <div class="list-element">
    <a href="/id/4463838" class="list-element__title">ООО "Кех Екоммерц"</a>
    <span>ИНН: 7710668349</span>
    <span>ОГРН: 5077746422859</span>
  </div>
  <div class="list-element">
    <a href="/id/7849910" class="list-element__title">ООО "АКС"</a>
    <span>ИНН: 7107109003</span>
    <span>ОГРН: 1157154022849</span>
  </div>
</body></html>
"""

_CARD_HTML = """
<html>
  <head>
    <title>ООО "Кех Екоммерц" Москва (ИНН 7710668349) адрес, телефон</title>
  </head>
  <body>
    <div>ОГРН 5077746422859</div>
    <div>Статус: Действующая организация</div>
    <div>Генеральный директор: Иванов Иван Иванович</div>
    <div>Учредитель: Петров Петр Петрович</div>
  </body>
</html>
"""


def test_is_zero_results():
    assert _is_zero_results("По запросу было найдено 0 результатов")
    assert not _is_zero_results("Найдено 12 результатов")


def test_extract_search_candidates():
    candidates = _extract_search_candidates(_SEARCH_HTML)
    assert len(candidates) == 2
    assert candidates[0]["inn"] == "7710668349"
    assert candidates[0]["ogrn"] == "5077746422859"
    assert candidates[0]["url"].endswith("/id/4463838")


def test_select_best_candidate_by_inn():
    candidates = _extract_search_candidates(_SEARCH_HTML)
    selected = _select_best_candidate(candidates, inn="7710668349", ogrn=None, name=None)
    assert selected is not None
    assert selected["inn"] == "7710668349"
    assert selected["score"] == 1.0
    assert selected["match_method"] == "inn"


def test_parse_company_page():
    parsed = _parse_company_page(_CARD_HTML, fallback_name=None)
    assert parsed["name"] == 'ООО "Кех Екоммерц" Москва'
    assert parsed["inn"] == "7710668349"
    assert parsed["ogrn"] == "5077746422859"
    assert parsed["status"] == "active"
    assert parsed["entity_type"] == "ЮЛ"
    assert parsed["director_name"] == "Иванов Иван Иванович"
    assert parsed["founders"] == ["Петров Петр Петрович"]


def test_map_rusprofile_to_registries():
    result = CheckResult(
        registry="rusprofile",
        found=True,
        status="active",
        details={
            "name": "ООО Тест",
            "inn": "5501234567",
            "ogrn": "1155500001234",
            "entity_type": "ЮЛ",
            "match_method": "name",
            "director_name": "Иванов Иван Иванович",
            "director_position": "Генеральный директор",
            "founders": ["Петров Петр Петрович"],
            "source_url": "https://www.rusprofile.ru/id/1",
        },
    )
    mapped = map_rusprofile_to_registries(result)
    assert mapped["dadata_fns"]["found"] is True
    assert mapped["dadata_fns"]["status"] == "ACTIVE"
    assert mapped["dadata_fns"]["details"]["inn"] == "5501234567"
    assert mapped["dadata_fns"]["details"]["match_method"] == "name"
    assert mapped["dadata_fns"]["details"]["director_name"] == "Иванов Иван Иванович"
    assert mapped["dadata_fns"]["details"]["founders"] == ["Петров Петр Петрович"]
