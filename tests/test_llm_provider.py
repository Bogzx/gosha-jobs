"""Tests for the switchable LLM provider port (gosha/llm.py)."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from gosha import llm

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


@pytest.fixture(autouse=True)
def _no_ambient_llm_env(monkeypatch):
    # A developer's shell (or the prod .env) must not pick the provider here.
    for var in ("LLM_PROVIDER", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
                "OPENROUTER_API_KEY", "OPENROUTER_MODEL"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.asyncio
@respx.mock
async def test_deepseek_used_when_key_set(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")

    route = respx.post(DEEPSEEK_URL).mock(
        return_value=Response(200, json={
            "choices": [{"message": {"content": "  Letter from DeepSeek. "}}]
        })
    )

    assert llm.active_provider() == "deepseek"
    assert await llm.generate("Write a letter") == "Letter from DeepSeek."

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer ds-key"
    assert "x-title" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["messages"] == [{"role": "user", "content": "Write a letter"}]


@pytest.mark.asyncio
@respx.mock
async def test_deepseek_model_is_configurable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    route = respx.post(DEEPSEEK_URL).mock(
        return_value=Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )

    await llm.generate("x")
    assert json.loads(route.calls[0].request.content)["model"] == "deepseek-v4-pro"


def test_deepseek_key_beats_openrouter_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    assert llm.active_provider() == "deepseek"

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    assert llm.active_provider() == "openrouter"


@pytest.mark.asyncio
@respx.mock
async def test_deepseek_error_returns_none(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    respx.post(DEEPSEEK_URL).mock(return_value=Response(401, text="bad key"))

    assert await llm.generate("x") is None


@pytest.mark.asyncio
async def test_deepseek_without_key_returns_none(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    assert await llm.generate("x") is None


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_used_when_key_set(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    route = respx.post(OPENROUTER_URL).mock(
        return_value=Response(200, json={
            "choices": [{"message": {"content": "Generated letter."}}]
        })
    )

    result = await llm.generate("Write a letter")
    assert result == "Generated letter."

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer or-key"
    body = json.loads(request.content)
    assert body["model"] == "deepseek/deepseek-v4-flash"
    assert body["messages"][0]["content"] == "Write a letter"


@pytest.mark.asyncio
async def test_gemini_used_without_openrouter_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    called = {}

    async def fake_gemini(prompt: str) -> str:
        called["prompt"] = prompt
        return "From Gemini"

    monkeypatch.setattr(llm, "generate_gemini", fake_gemini)
    result = await llm.generate("Hello")
    assert result == "From Gemini"
    assert called["prompt"] == "Hello"


@pytest.mark.asyncio
async def test_explicit_provider_overrides_autodetect(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    async def fake_gemini(prompt: str) -> str:
        return "Gemini wins"

    monkeypatch.setattr(llm, "generate_gemini", fake_gemini)
    assert await llm.generate("x") == "Gemini wins"


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_error_returns_none(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    respx.post(OPENROUTER_URL).mock(return_value=Response(500, text="boom"))

    assert await llm.generate_openrouter("x") is None


@pytest.mark.asyncio
@respx.mock
async def test_gemini_falls_through_models(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*gemini-3-flash-preview.*").mock(
        return_value=Response(429, text="rate limited")
    )
    respx.post(url__regex=r"https://generativelanguage\.googleapis\.com/.*").mock(
        return_value=Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "Second model wins"}]}}]
        })
    )
    assert await llm.generate_gemini("x") == "Second model wins"
