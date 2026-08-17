"""LLM provider port: one `generate()` entry point, pluggable providers.

Providers are adapters around external APIs. Selection:
  LLM_PROVIDER=gemini|openrouter forces one; otherwise OpenRouter is used
  whenever OPENROUTER_API_KEY is set, falling back to Gemini.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4-flash"

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
    if provider in ("gemini", "openrouter"):
        return provider
    return "openrouter" if os.getenv("OPENROUTER_API_KEY") else "gemini"


async def generate(prompt: str) -> str | None:
    """Generate text with the configured provider; None on failure."""
    if active_provider() == "openrouter":
        return await generate_openrouter(prompt)
    return await generate_gemini(prompt)


async def generate_openrouter(prompt: str) -> str | None:
    """OpenRouter's OpenAI-compatible chat completions API."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        log.error("OPENROUTER_API_KEY not set — OpenRouter unavailable")
        return None
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 2048,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                OPENROUTER_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "X-Title": "GOSHA Jobs",
                },
            )
        if resp.status_code != 200:
            log.error("OpenRouter error %d: %s", resp.status_code, resp.text[:300])
            return None
        choices = resp.json().get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
        return content.strip() if content else None
    except httpx.HTTPError as exc:
        log.error("OpenRouter request failed: %s", exc)
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
