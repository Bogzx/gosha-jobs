"""Fixtures shared by the adapter tests."""

from __future__ import annotations

import pytest

from gosha.scrapers import base


@pytest.fixture(autouse=True)
def no_browser_fallback(monkeypatch):
    """The browser-fingerprint retry is opt-in (SCRAPE_BROWSER_FALLBACK) and
    off here, whatever the environment says. Where a test turns it on, a
    mocked 403 must still not reach the real board: the retry goes through
    curl_cffi, which respx does not intercept."""
    monkeypatch.delenv("SCRAPE_BROWSER_FALLBACK", raising=False)
    monkeypatch.setattr(base, "_browser_hosts", set())

    async def refuse(_request):
        return None

    monkeypatch.setattr(base, "_browser_get", refuse)
