import pytest

from src.enrichment.registries import fssp as fssp_module


def test_extract_payload_html_unwraps_json_fragment():
    raw = (
        '({"data":"<div class=\\"b-search-message t-warning m_b2 hgt2-re\\">'
        "<h4>Ваш запрос обрабатывается</h4><p>Попробуйте позже</p></div>\"})"
    )

    html = fssp_module._extract_payload_html(raw)

    assert "Ваш запрос обрабатывается" in html
    assert "Попробуйте позже" in html


def test_parse_html_reads_result_table_and_sum():
    html = """
    <table id="resultList">
      <tr><th>Case</th><th>Debt</th></tr>
      <tr><td>IP-1</td><td>1 234,50 руб.</td></tr>
      <tr><td>IP-2</td><td>765,50 руб.</td></tr>
    </table>
    """

    result = fssp_module._parse_html(html, "fssp")

    assert result.found is True
    assert result.status == "has_executions"
    assert result.details["executions_count"] == 2
    assert result.details["total_debt_rub"] == 2000.0


def test_playwright_proxy_from_url_uses_separate_auth_fields():
    proxy = fssp_module._playwright_proxy_from_url("http://user:pass@192.0.2.1:26076")

    assert proxy == {
        "server": "socks5://192.0.2.1:26076",
        "username": "user",
        "password": "pass",
    }


@pytest.mark.asyncio
async def test_try_httpx_returns_processing_error_after_retries(monkeypatch, no_sleep):
    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_submit(client, *, params):  # noqa: ARG001
        return """
        <div class="b-search-message t-warning m_b2 hgt2-re">
          <div class="b-search-message__text">
            <h4>Ваш запрос обрабатывается</h4>
            <p>Попробуйте позже</p>
          </div>
        </div>
        """

    monkeypatch.setattr(fssp_module.httpx, "AsyncClient", lambda *args, **kwargs: DummyClient())
    monkeypatch.setattr(fssp_module, "_iter_fssp_proxy_urls", lambda: iter(["socks5://proxy:1080"]))
    monkeypatch.setattr(fssp_module, "_submit_httpx_search", fake_submit)
    monkeypatch.setattr(fssp_module.settings, "fssp_processing_max_retries", 1)
    monkeypatch.setattr(fssp_module.settings, "fssp_processing_retry_delay_seconds", 1800)

    result = await fssp_module.FsspChecker()._try_httpx(inn="5050068324")

    assert result is not None
    assert result.error == fssp_module._PROCESSING_ERROR


@pytest.mark.asyncio
async def test_try_httpx_returns_captcha_error_without_playwright_fallback(monkeypatch):
    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_submit(client, *, params):  # noqa: ARG001
        return '<div id="captcha-popup"><h2>Введите код с картинки</h2></div>'

    monkeypatch.setattr(fssp_module.httpx, "AsyncClient", lambda *args, **kwargs: DummyClient())
    monkeypatch.setattr(fssp_module, "_iter_fssp_proxy_urls", lambda: iter(["socks5://proxy:1080"]))
    monkeypatch.setattr(fssp_module, "_submit_httpx_search", fake_submit)
    monkeypatch.setattr(fssp_module.settings, "fssp_processing_max_retries", 0)

    result = await fssp_module.FsspChecker()._try_httpx(inn="5050068324")

    assert result is not None
    assert result.error == fssp_module._CAPTCHA_ERROR
