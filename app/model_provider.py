"""Model provider abstraction (D16).

Two implementations behind a common Protocol:
  - GeminiProvider  — Google Gemini via the `google-genai` SDK, used in production.
  - OllamaProvider — local HTTP call to Ollama for Apple-Silicon dev.

Providers are pure text-in / text-out. They do NOT retry on failure; the
generation pipeline (Step 5) handles retries at the candidate level. They do
NOT validate length, non-emptiness, or copyright safety; that's Step 5 too.
They just return whatever the model said in the `{"fact": "..."}` JSON shape.
"""
from __future__ import annotations

import json
from typing import Protocol

import httpx
from google import genai
from google.genai import types

from app.config import settings


# Exact v1 prompt from Backend Architecture. The {extract} placeholder is the
# only interpolation point — anything else in the prompt is fixed.
V1_PROMPT = """You are generating a single fact for a daily history notification app.

Source article:
{extract}

Task: Extract one genuinely surprising fact from this article. Write it as
one sentence, in your own words, under 280 characters. Do not copy phrasing
from the source. Do not hedge ("it is said that", "reportedly"). State the
fact directly as if you are a knowledgeable friend mentioning it.

Respond with JSON only: {{"fact": "your sentence here"}}"""


class ModelProviderError(Exception):
    """Raised when the model call fails, returns malformed JSON, or empty output.

    The message carries provider, model, and a snippet of what was actually
    returned so logs are debuggable without re-running the call.
    """


class ModelProvider(Protocol):
    async def extract_fact(self, article_extract: str) -> str: ...


def _parse_fact_json(raw: str, *, provider: str, model: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelProviderError(
            f"{provider}:{model} returned non-JSON output: {raw!r}"
        ) from exc
    fact = data.get("fact") if isinstance(data, dict) else None
    if not isinstance(fact, str) or not fact.strip():
        raise ModelProviderError(
            f"{provider}:{model} returned JSON without a non-empty 'fact' string: {data!r}"
        )
    return fact.strip()


class GeminiProvider:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def extract_fact(self, article_extract: str) -> str:
        prompt = V1_PROMPT.format(extract=article_extract)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {"fact": {"type": "STRING"}},
                "required": ["fact"],
            },
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:  # SDK raises a variety of google.genai errors
            raise ModelProviderError(
                f"gemini:{self._model} API call failed: {exc!r}"
            ) from exc

        raw = (response.text or "").strip()
        if not raw:
            raise ModelProviderError(
                f"gemini:{self._model} returned empty response text"
            )
        return _parse_fact_json(raw, provider="gemini", model=self._model)


class OllamaProvider:
    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def extract_fact(self, article_extract: str) -> str:
        prompt = V1_PROMPT.format(extract=article_extract)
        payload = {
            "model": self._model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/generate", json=payload
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                f"ollama:{self._model} HTTP call failed: {exc!r}"
            ) from exc

        raw = (body.get("response") or "").strip()
        if not raw:
            raise ModelProviderError(
                f"ollama:{self._model} returned empty 'response' field: {body!r}"
            )
        return _parse_fact_json(raw, provider="ollama", model=self._model)


def get_provider() -> ModelProvider:
    """Build the configured provider, failing loudly on missing required vars.

    We validate GEMINI_API_KEY at build time because the production boot path
    wires this up once and we want a clear startup error if the key is missing.
    For Ollama we defer the reachability check to the first call — local dev
    may start the app before `ollama serve`.
    """
    provider = settings.MODEL_PROVIDER
    if provider == "gemini":
        if not settings.GEMINI_API_KEY:
            raise ModelProviderError(
                "MODEL_PROVIDER=gemini but GEMINI_API_KEY is not set"
            )
        return GeminiProvider(
            api_key=settings.GEMINI_API_KEY, model=settings.GEMINI_MODEL
        )
    if provider == "ollama":
        return OllamaProvider(
            base_url=settings.OLLAMA_BASE_URL, model=settings.OLLAMA_MODEL
        )
    raise ModelProviderError(f"unknown MODEL_PROVIDER={provider!r}")
