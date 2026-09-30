"""LLM provider port: one `generate()` entry point, pluggable providers.

Providers are adapters around external APIs. Selection:
  LLM_PROVIDER=deepseek|openrouter|gemini forces one; otherwise the first
  provider with a key wins: DEEPSEEK_API_KEY, then OPENROUTER_API_KEY, then
  Gemini.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4-flash"

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"

PROVIDERS = ("deepseek", "openrouter", "gemini")

GEMINI_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemma-4-31b-it",
]


def active_provider() -> str:
    provider = os.getenv("LLM_PROVIDER", "").lower().strip()
    if provider in PROVIDERS:
        return provider
    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"
    return "openrouter" if os.getenv("OPENROUTER_API_KEY") else "gemini"


async def generate(prompt: str) -> str | None:
    """Generate text with the configured provider; None on failure."""
    provider = active_provider()
    if provider == "deepseek":
        return await generate_deepseek(prompt)
    if provider == "openrouter":
        return await generate_openrouter(prompt)
    return await generate_gemini(prompt)


async def generate_deepseek(prompt: str) -> str | None:
    """DeepSeek's own OpenAI-compatible chat completions API."""
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        log.error("DEEPSEEK_API_KEY not set — DeepSeek unavailable")
        return None
    return await _chat_completion(
        "DeepSeek",
        DEEPSEEK_URL,
        api_key,
        os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
        prompt,
    )


async def generate_openrouter(prompt: str) -> str | None:
    """OpenRouter's OpenAI-compatible chat completions API."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        log.error("OPENROUTER_API_KEY not set — OpenRouter unavailable")
        return None
    return await _chat_completion(
        "OpenRouter",
        OPENROUTER_URL,
        api_key,
        os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        prompt,
        extra_headers={"X-Title": "GOSHA Jobs"},
    )


async def _chat_completion(
    name: str,
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    extra_headers: dict[str, str] | None = None,
) -> str | None:
    """POST one user message to an OpenAI-compatible endpoint; None on failure."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 2048,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", **(extra_headers or {})},
            )
        if resp.status_code != 200:
            log.error("%s error %d: %s", name, resp.status_code, resp.text[:300])
            return None
        choices = resp.json().get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
        return content.strip() if content else None
    except httpx.HTTPError as exc:
        log.error("%s request failed: %s", name, exc)
        return None


async def generate_gemini(prompt: str) -> str | None:
    """Google Gemini API, trying each model in GEMINI_MODELS until one works."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        log.error("GEMINI_API_KEY not set — Gemini unavailable")
        return None

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 2048,
        },
    }

    # The key goes in a header, not the query string: URLs are logged by
    # proxies, browsers and error trackers, and this one bills real money.
    headers = {"x-goog-api-key": api_key}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for model in GEMINI_MODELS:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent"
            )
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    log.warning(
                        "Gemini model %s returned %d — trying next",
                        model, resp.status_code,
                    )
                    continue
                data = resp.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    continue
                parts = (candidates[0].get("content") or {}).get("parts") or []
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return text
            except httpx.HTTPError as exc:
                log.warning("Gemini model %s failed: %s — trying next", model, exc)
    return None
