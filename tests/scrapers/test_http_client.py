"""The adapters' HTTP client: with SCRAPE_BROWSER_FALLBACK on, a 403'd GET
is retried with a browser's TLS fingerprint (BestJobs refuses Python's
from some networks). Off by default."""

from __future__ import annotations

import httpx
import respx

from gosha.scrapers import base


def _browser(monkeypatch, status: int = 200, body: bytes = b'{"items": []}'):
    monkeypatch.setenv("SCRAPE_BROWSER_FALLBACK", "true")
    calls: list[str] = []

    async def fake(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, content=body, request=request)

    monkeypatch.setattr(base, "_browser_get", fake)
    return calls


@respx.mock
async def test_403_is_retried_with_a_browser_fingerprint(monkeypatch):
    plain = respx.get("https://api.board.test/jobs").mock(return_value=httpx.Response(403))
    calls = _browser(monkeypatch)

    async with base.http_client() as client:
        first = await client.get("https://api.board.test/jobs", params={"keyword": "java"})
        second = await client.get("https://api.board.test/jobs")

    assert first.status_code == 200 and first.json() == {"items": []}
    assert calls[0] == "https://api.board.test/jobs?keyword=java"
    # The host is remembered: the second request skips the doomed plain try.
    assert second.status_code == 200
    assert plain.call_count == 1 and len(calls) == 2


@respx.mock
async def test_other_statuses_and_hosts_stay_on_plain_httpx(monkeypatch):
    respx.get("https://ok.test/").mock(return_value=httpx.Response(200, text="hi"))
    respx.get("https://gone.test/").mock(return_value=httpx.Response(404))
    calls = _browser(monkeypatch)

    async with base.http_client() as client:
        assert (await client.get("https://ok.test/")).text == "hi"
        assert (await client.get("https://gone.test/")).status_code == 404

    assert calls == []
    assert base._browser_hosts == set()


@respx.mock
async def test_a_retry_that_is_refused_too_keeps_the_original_403(monkeypatch):
    respx.get("https://blocked.test/").mock(return_value=httpx.Response(403, text="no"))
    _browser(monkeypatch, status=403)

    async with base.http_client() as client:
        resp = await client.get("https://blocked.test/")

    assert resp.status_code == 403 and resp.text == "no"
    assert "blocked.test" not in base._browser_hosts


@respx.mock
async def test_a_failed_retry_keeps_the_original_403(monkeypatch):
    # conftest's stand-in returns None, as a curl_cffi failure does.
    monkeypatch.setenv("SCRAPE_BROWSER_FALLBACK", "1")
    respx.get("https://blocked.test/").mock(return_value=httpx.Response(403))
    async with base.http_client() as client:
        assert (await client.get("https://blocked.test/")).status_code == 403


@respx.mock
async def test_fallback_is_off_by_default(monkeypatch):
    """Getting around a board's bot protection is the operator's call."""
    calls: list[str] = []

    async def fake(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, request=request)

    monkeypatch.setattr(base, "_browser_get", fake)
    respx.get("https://blocked.test/").mock(return_value=httpx.Response(403))
    assert not base.browser_fallback_enabled()
    async with base.http_client() as client:
        assert (await client.get("https://blocked.test/")).status_code == 403
    assert calls == []
