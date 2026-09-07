from src.collectors.base import RawCompany
from src.enrichment.website_scanner import extract_website_candidates


def test_extract_website_candidates_filters_platform_tracking_urls():
    raw = RawCompany(
        source="yandex",
        name_raw="Абсолют",
        contacts_json={
            "websites": [
                "https://yandex.ru/an/count/WheejI_zOoVX2LcN0cKL0DCcbRxMbd0~2",
                "https://avatars.mds.yandex.net/get-altay/18147375/2a0000019c7b5292/%s",
                "https://vk.com/absolut155",
                "https://example-plumber.ru/about",
            ]
        },
        raw_payload={
            "urls": [
                "my-plumber.ru",
                "https://2gis.ru/omsk/firm/70000001029998371",
            ]
        },
    )

    candidates = extract_website_candidates(raw)

    assert "https://example-plumber.ru/" in candidates
    assert "https://my-plumber.ru/" in candidates
    assert all("yandex.ru" not in url for url in candidates)
    assert all("mds.yandex.net" not in url for url in candidates)
    assert all("vk.com" not in url for url in candidates)
    assert all("2gis.ru" not in url for url in candidates)
