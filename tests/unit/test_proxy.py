"""Unit tests for ProxyManager — round-robin, file loading, fallback."""

import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.proxy import ProxyManager, _normalise


# ── _normalise helper ───────────────────────────────────────────────────────


def test_normalise_adds_http_scheme():
    assert _normalise("host:1234") == "http://host:1234"


def test_normalise_preserves_http():
    assert _normalise("http://user:pass@host:1234") == "http://user:pass@host:1234"


def test_normalise_preserves_socks5():
    assert _normalise("socks5://user:pass@host:1234") == "socks5://user:pass@host:1234"


# ── ProxyManager.get_next ───────────────────────────────────────────────────


def _make_manager(proxies: list[str]) -> ProxyManager:
    """Create a ProxyManager with a pre-loaded proxy list, bypassing file I/O."""
    with patch("src.proxy.settings") as mock_settings:
        mock_settings.proxy_file = ""
        mock_settings.proxy_url = ""
        mgr = ProxyManager()
    mgr._proxies = proxies
    mgr._index = 0
    return mgr


def test_get_next_returns_none_when_empty():
    mgr = _make_manager([])
    assert mgr.get_next() is None


def test_get_next_returns_single_proxy():
    mgr = _make_manager(["http://proxy1:8080"])
    assert mgr.get_next() == "http://proxy1:8080"


def test_round_robin_cycles():
    proxies = ["http://proxy1:8080", "http://proxy2:8080", "http://proxy3:8080"]
    mgr = _make_manager(proxies)
    results = [mgr.get_next() for _ in range(6)]
    assert results == proxies + proxies


def test_playwright_proxy_returns_dict():
    mgr = _make_manager(["http://proxy1:8080"])
    result = mgr.playwright_proxy()
    assert result == {"server": "http://proxy1:8080"}


def test_playwright_proxy_returns_none_when_empty():
    mgr = _make_manager([])
    assert mgr.playwright_proxy() is None


def test_count_property():
    mgr = _make_manager(["http://p1:1", "http://p2:2"])
    assert mgr.count == 2


# ── Loading from file ───────────────────────────────────────────────────────


def test_load_from_file():
    content = "# comment\nhttp://p1:8080\nhttp://p2:8080\n\n# another comment\nhttp://p3:8080\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(content)
        tmp_path = f.name

    with patch("src.proxy.settings") as mock_settings:
        mock_settings.proxy_file = tmp_path
        mock_settings.proxy_url = ""
        mgr = ProxyManager()

    assert mgr.count == 3
    assert mgr.get_next() == "http://p1:8080"

    Path(tmp_path).unlink()


def test_load_normalises_bare_host():
    content = "proxy.example.com:8080\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(content)
        tmp_path = f.name

    with patch("src.proxy.settings") as mock_settings:
        mock_settings.proxy_file = tmp_path
        mock_settings.proxy_url = ""
        mgr = ProxyManager()

    assert mgr.get_next() == "http://proxy.example.com:8080"
    Path(tmp_path).unlink()


def test_fallback_to_proxy_url_when_file_empty():
    with patch("src.proxy.settings") as mock_settings:
        mock_settings.proxy_file = ""
        mock_settings.proxy_url = "http://single-proxy:3128"
        mgr = ProxyManager()

    assert mgr.count == 1
    assert mgr.get_next() == "http://single-proxy:3128"


def test_file_not_found_falls_back_to_proxy_url():
    with patch("src.proxy.settings") as mock_settings:
        mock_settings.proxy_file = "/nonexistent/path/proxies.txt"
        mock_settings.proxy_url = "http://fallback:3128"
        mgr = ProxyManager()

    assert mgr.get_next() == "http://fallback:3128"


def test_no_proxy_configured():
    with patch("src.proxy.settings") as mock_settings:
        mock_settings.proxy_file = ""
        mock_settings.proxy_url = ""
        mgr = ProxyManager()

    assert mgr.count == 0
    assert mgr.get_next() is None


# ── Thread safety ───────────────────────────────────────────────────────────


def test_thread_safe_round_robin():
    """Multiple threads should collectively consume each proxy once per cycle."""
    proxies = [f"http://proxy{i}:8080" for i in range(5)]
    mgr = _make_manager(proxies)

    collected = []
    lock = threading.Lock()

    def worker():
        val = mgr.get_next()
        with lock:
            collected.append(val)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Should have collected 10 results, each being a valid proxy
    assert len(collected) == 10
    for proxy in collected:
        assert proxy in proxies
