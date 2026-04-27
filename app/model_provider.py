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
from typing import Optional, Protocol

import httpx
from google import genai
from google.genai import types

from app.config import settings


# Exact v1 prompt from Backend Architecture. The {extract} placeholder is the
# only interpolation point — anything else in the prompt is fixed.
#
# DO NOT MODIFY V1_PROMPT. It's the calibration baseline for every prompt A/B
# (Sessions 13b/d/e). Editing it would invalidate every comparison that's
# been done against rated v1 data. New iterations live as new constants
# (V3_PROMPT, V4_PROMPT, ...) and are activated via PROMPT_VERSION.
V1_PROMPT = """You are generating a single fact for a daily history notification app.

Source article:
{extract}

Task: Extract one genuinely surprising fact from this article. Write it as
one sentence, in your own words, under 280 characters. Do not copy phrasing
from the source. Do not hedge ("it is said that", "reportedly"). State the
fact directly as if you are a knowledgeable friend mentioning it.

Respond with JSON only: {{"fact": "your sentence here"}}"""


# Step 13e: V2_PROMPT removed.
#
# The v2 designation is contaminated. v2 ran on Gemini 2.5 Flash + REST-only
# extracts (~800 chars) + a non-functional pre-filter. Will's review of the
# resulting batch surfaced six specific failure clusters (meta articles
# slipping through, stub articles producing stub facts, tautological so-whats,
# wrong angles even with explicit rules, list-article repeats, buried ledes)
# that v3 fixes by changing both the model AND the source AND the filter AND
# the prompt simultaneously. Keeping v2 in the registry would invite anyone
# pointing PROMPT_VERSION=v2 at the new model+source+filter and producing
# results that aren't comparable to the (deleted) v2 batch OR to v3.
#
# v2 facts in pool were deleted in migration `0b3a8f2e1c4d`. The historical
# V2_PROMPT text is preserved verbatim in the Session 13d Claude Code Log
# entry and in the Decisions Log discussion of D27.


# V3_PROMPT (Step 13e). Fixes six failure clusters surfaced by Will's review
# of the v2 batch:
#   1. Tautological so-whats — Rule 3 forbids "X did Y, reflecting Y" loops.
#   2. Wrong angle even with explicit rules — Rule 1 names the "huh, really?"
#      target explicitly and lists adjacent angles (etymology, provenance,
#      forgotten cultural exchange) that v2 missed.
#   3. Buried lede — Rule 4 mandates leading with the surprising thing as the
#      MAIN clause, not a dependent clause.
#   4. Filler intensifiers — Rule 8 expanded to include "fascinatingly",
#      "surprisingly".
#   5. Higher char ceiling — Rule 7 raised to 200-350 typical, hard cap 400
#      (was 100-250 / 280 in v2). Two-sentence facts need room.
#   6. Stub/list/meta articles — handled by the pre-filter (title regex +
#      MIN_EXTRACT_CHARS + _looks_infoboxy in app/wikipedia.py and
#      app/generation.py), not by the prompt.
V3_PROMPT = """You are crafting a single fact for a daily history app aimed at a smart but non-specialist English-speaking reader.

You will be given a Wikipedia article extract. Your task is to surface the single most interesting thing in the article and state it in your own words.

Rules:
1. Find the most interesting thing in the article — the angle a reader would tell a friend about. The thing that makes someone go "huh, really?" History-adjacent angles are welcome and often best: etymology, the origin of a word or place name, how a technique was discovered, why a place got its current borders, the surprising provenance of an everyday object, a forgotten cultural exchange. Not the most prominent fact, not the formal definition, not the date of founding.

2. Land the so-what. The reader should finish the fact understanding why this matters — what it changed, who was affected, what the consequence was.

3. The so-what must be a SEPARATE fact from the headline. If you find yourself writing "X did Y, reflecting/showing/demonstrating Y" or "the name means Z, reflecting the desire for Z" — start over. The consequence must be something the reader couldn't have inferred from the headline alone. Tautologies are forbidden.

4. Lead with the surprising thing. If the article contains a shocking detail (a city was destroyed, a princess was 12, a scholar was executed, a rebellion was led by an unexpected figure), that detail should be what the sentence is ABOUT — not a dependent clause attached to a more boring main clause. If you write "After the city was destroyed in 1702, this house was built" — restructure so "the city was destroyed in 1702" is the fact.

5. Assume the reader is unfamiliar with the topic. Briefly ground anything niche — a place, a title, a people, a regnal name — so the fact lands without requiring outside knowledge. One short clause is enough.

6. State the fact in your own words. Do not copy phrasing from the source.

7. 1-2 sentences. The first sentence states the fact. A second sentence is allowed only when it lands the consequence, the timeline, or the context that the first sentence couldn't carry. Do NOT add a second sentence as filler. Aim for 200-350 characters total. Hard cap at 400.

8. No filler intensifiers ("incredibly", "astonishingly", "remarkably", "fascinatingly", "surprisingly") and no lecturing tone. Let the fact be surprising on its own.

Return JSON with a single field "fact" containing the sentence(s) and nothing else.

Article extract:
{extract}
"""


# V4_PROMPT (Step 13f). Targets the four bottom-5 v3 failure clusters Will
# surfaced in the v3 calibration round. v3 hit 50% approve (Criterion 1: PASS)
# but the bottom-5 (rating=2) v3 facts shared four distinct execution issues:
#   1. Vague trailing endings ("survived well into the Roman era" — closing
#      clause that gestures at continuation instead of landing a consequence).
#   2. First/second-half pivot (sentence 2 arrives at an unrelated claim).
#   3. Overreach (article said "contributed to", fact escalated to "shattered").
#   4. "Among other things" — incomplete framing that signals laziness.
# Two new rules added vs V3_PROMPT (Rule 5 close-cleanly, Rule 6
# stay-within-source). All other rules preserved verbatim from V3 — the
# upstream rules were working; this round is execution polish, not structure
# rewrite. Sample size for v4 calibration is intentionally smaller (n=30) per
# agreement: this is the last prompt iteration; if v4 isn't a clear
# qualitative win on the bottom-5 patterns, ship v3 and move to Step 14.
V4_PROMPT = """You are crafting a single fact for a daily history app aimed at a smart but non-specialist English-speaking reader.

You will be given a Wikipedia article extract. Your task is to surface the single most interesting thing in the article and state it in your own words.

Rules:
1. Find the most interesting thing in the article — the angle a reader would tell a friend about. The thing that makes someone go "huh, really?" History-adjacent angles are welcome and often best: etymology, the origin of a word or place name, how a technique was discovered, why a place got its current borders, the surprising provenance of an everyday object, a forgotten cultural exchange. Not the most prominent fact, not the formal definition, not the date of founding.

2. Land the so-what. The reader should finish the fact understanding why this matters — what it changed, who was affected, what the consequence was.

3. The so-what must be a SEPARATE fact from the headline. If you find yourself writing "X did Y, reflecting/showing/demonstrating Y" or "the name means Z, reflecting the desire for Z" — start over. The consequence must be something the reader couldn't have inferred from the headline alone. Tautologies are forbidden.

4. Lead with the surprising thing. If the article contains a shocking detail (a city was destroyed, a scholar was executed, a rebellion was led by an unexpected figure), that detail should be what the sentence is ABOUT — not a dependent clause attached to a more boring main clause.

5. Close cleanly. The closing clause must add a specific stake, named consequence, or concrete detail — not a vague continuation. Forbidden endings: "survived well into...", "continued to influence...", "shaped X for centuries", "remains an important part of...", "among other things...", "and various other...". If you use a second sentence, it must directly pay off, ground, or complete the first — not pivot to an unrelated detail. If you can't end specifically, write a shorter fact.

6. Stay within the source. If the article says X "contributed to" Y or "was associated with" Y, don't escalate to "directly caused" or "shattered" or "transformed". Match the article's level of certainty. Don't speculate beyond what the source supports.

7. Assume the reader is unfamiliar with the topic. Briefly ground anything niche — a place, a title, a people, a regnal name — so the fact lands without requiring outside knowledge. One short clause is enough.

8. State the fact in your own words. Do not copy phrasing from the source.

9. 1-2 sentences. The first sentence states the fact. A second sentence is allowed only when it lands the consequence, the timeline, or the context that the first sentence couldn't carry. Do NOT add a second sentence as filler. Aim for 200-350 characters total. Hard cap at 400.

10. No filler intensifiers ("incredibly", "astonishingly", "remarkably", "fascinatingly", "surprisingly") and no lecturing tone. Let the fact be surprising on its own.

Return JSON with a single field "fact" containing the sentence(s) and nothing else.

Article extract:
{extract}
"""


# V4_1_PROMPT (Step 13f addition). Minimal-diff tonal tweak over V4 to land
# the morning-fact framing — the daily fact lands in users' phones first
# thing alongside news + work notifications, so the voice should read more
# like "knowledgeable friend over coffee" than "encyclopedia entry". Two
# changes vs V4:
#   1. Opening adds a one-line audience-context sentence framing the morning
#      delivery and naming the target voice.
#   2. Rule 10 is rewritten as a positive voice directive ("warm,
#      conversational, lightly playful") while keeping the v3/v4
#      filler-intensifier ban intact ("incredibly", "astonishingly",
#      "remarkably", "fascinatingly", "surprisingly") because those were a
#      v1 stylistic-tic, not playfulness. The playfulness comes from word
#      choice, not from telling the reader to be amazed.
# All other rules (1-9) preserved verbatim from V4. Anti-tautology, anti-
# buried-lede, close-cleanly, and stay-within-source are all still in force.
V4_1_PROMPT = """You are crafting a single fact for a daily history app aimed at a smart but non-specialist English-speaking reader. The fact will land in their morning notifications, so write like a knowledgeable friend mentioning something genuinely cool over coffee — warm, slightly playful, never lecturing.

You will be given a Wikipedia article extract. Your task is to surface the single most interesting thing in the article and state it in your own words.

Rules:
1. Find the most interesting thing in the article — the angle a reader would tell a friend about. The thing that makes someone go "huh, really?" History-adjacent angles are welcome and often best: etymology, the origin of a word or place name, how a technique was discovered, why a place got its current borders, the surprising provenance of an everyday object, a forgotten cultural exchange. Not the most prominent fact, not the formal definition, not the date of founding.

2. Land the so-what. The reader should finish the fact understanding why this matters — what it changed, who was affected, what the consequence was.

3. The so-what must be a SEPARATE fact from the headline. If you find yourself writing "X did Y, reflecting/showing/demonstrating Y" or "the name means Z, reflecting the desire for Z" — start over. The consequence must be something the reader couldn't have inferred from the headline alone. Tautologies are forbidden.

4. Lead with the surprising thing. If the article contains a shocking detail (a city was destroyed, a scholar was executed, a rebellion was led by an unexpected figure), that detail should be what the sentence is ABOUT — not a dependent clause attached to a more boring main clause.

5. Close cleanly. The closing clause must add a specific stake, named consequence, or concrete detail — not a vague continuation. Forbidden endings: "survived well into...", "continued to influence...", "shaped X for centuries", "remains an important part of...", "among other things...", "and various other...". If you use a second sentence, it must directly pay off, ground, or complete the first — not pivot to an unrelated detail. If you can't end specifically, write a shorter fact.

6. Stay within the source. If the article says X "contributed to" Y or "was associated with" Y, don't escalate to "directly caused" or "shattered" or "transformed". Match the article's level of certainty. Don't speculate beyond what the source supports.

7. Assume the reader is unfamiliar with the topic. Briefly ground anything niche — a place, a title, a people, a regnal name — so the fact lands without requiring outside knowledge. One short clause is enough.

8. State the fact in your own words. Do not copy phrasing from the source.

9. 1-2 sentences. The first sentence states the fact. A second sentence is allowed only when it lands the consequence, the timeline, or the context that the first sentence couldn't carry. Do NOT add a second sentence as filler. Aim for 200-350 characters total. Hard cap at 400.

10. Voice: warm, conversational, lightly playful — like a friend telling you about something that delighted them, not a textbook narrating. Specific verbs over generic ones. No filler intensifiers ("incredibly", "astonishingly", "remarkably", "fascinatingly", "surprisingly") and no lecturing tone. Let the fact be surprising on its own; the playfulness comes from word choice, not from telling the reader to be amazed.

Return JSON with a single field "fact" containing the sentence(s) and nothing else.

Article extract:
{extract}
"""


# Registry of known prompt versions. v2 is deliberately absent (see V2 comment
# above). v4 added Step 13f; v4.1 added as a tonal variant of v4 (Step 13f
# addition). Add new versions here as they're built; get_active_prompt()
# resolves the active one by name and raises ValueError on unknown version
# so a stale Railway env var fails loudly rather than silently rolling back
# to v1.
_PROMPTS: dict[str, str] = {
    "v1": V1_PROMPT,
    "v3": V3_PROMPT,
    "v4": V4_PROMPT,
    "v4.1": V4_1_PROMPT,
}


def get_active_prompt(version: str | None = None) -> str:
    """Resolve the active prompt template by version string.

    Defaults to settings.PROMPT_VERSION when version is None. Raises
    ValueError on unknown version so misconfigurations fail loudly at the
    first generation call rather than silently emitting v1 output under a
    different label.
    """
    v = version if version is not None else settings.PROMPT_VERSION
    if v not in _PROMPTS:
        raise ValueError(
            f"Unknown PROMPT_VERSION={v!r}. Known: {sorted(_PROMPTS)}"
        )
    return _PROMPTS[v]


class ModelProviderError(Exception):
    """Raised when the model call fails, returns malformed JSON, or empty output.

    The message carries provider, model, and a snippet of what was actually
    returned so logs are debuggable without re-running the call.
    """


class ModelProvider(Protocol):
    async def extract_fact(
        self, article_extract: str, *, version: Optional[str] = None
    ) -> str:
        """Generate a fact from a Wikipedia article extract.

        Step 13e: optional `version` overrides the active prompt selection.
        Defaults to None which falls through to settings.PROMPT_VERSION via
        get_active_prompt(). The override path is for ops scripts (e.g.
        regenerate_with_v3.py) that force a non-default prompt without
        mutating the global settings object.
        """
        ...

    async def generate_text(self, prompt: str) -> str:
        """Run an arbitrary prompt through the model and return raw text.

        Step 14: parallel path to extract_fact for callers that want to
        provide their own prompt structure and parse their own response
        (e.g. app.judge which embeds a few-shot calibration prompt and
        parses its own JSON `{"score": ..., "reason": ...}` output).

        Implementations request JSON output mode where the SDK supports
        it (Gemini, Ollama) so the caller's `json.loads(...)` is safe.
        Implementations do NOT pass a response_schema — schema is the
        caller's responsibility.
        """
        ...


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

    async def extract_fact(
        self, article_extract: str, *, version: Optional[str] = None
    ) -> str:
        prompt = get_active_prompt(version).format(extract=article_extract)
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

    async def generate_text(self, prompt: str) -> str:
        """Step 14: bare-prompt JSON-mode call. No schema (caller-owned)."""
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            raise ModelProviderError(
                f"gemini:{self._model} generate_text API call failed: {exc!r}"
            ) from exc
        raw = (response.text or "").strip()
        if not raw:
            raise ModelProviderError(
                f"gemini:{self._model} generate_text returned empty response text"
            )
        return raw


class OllamaProvider:
    def __init__(self, base_url: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model

    async def extract_fact(
        self, article_extract: str, *, version: Optional[str] = None
    ) -> str:
        prompt = get_active_prompt(version).format(extract=article_extract)
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

    async def generate_text(self, prompt: str) -> str:
        """Step 14: bare-prompt JSON-mode call. No schema (caller-owned)."""
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
                f"ollama:{self._model} generate_text HTTP call failed: {exc!r}"
            ) from exc
        raw = (body.get("response") or "").strip()
        if not raw:
            raise ModelProviderError(
                f"ollama:{self._model} generate_text returned empty 'response': {body!r}"
            )
        return raw


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
