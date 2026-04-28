"""Unit tests for the module-level provider + judge singletons (Code Review
Fix 2 / P2.1).

The fix hoisted what was a per-call `provider = get_provider()` construction
inside `generate_one_pool_fact` up to a module-level lazy singleton mirroring
the existing `_judge` pattern. Both singletons share one underlying
`ModelProvider` instance — `_get_judge()` passes the shared instance into the
`Judge` constructor — so a process holds exactly one provider, not two.

These tests pin three contracts:
  1. `_get_provider()` is idempotent (same instance across calls; factory
     called once).
  2. `_get_judge()` reuses the same provider that `_get_provider()` returns.
  3. The factory (`get_provider` in app.generation's namespace) is invoked
     exactly once when both helpers are called in sequence.

Tests use sentinel objects rather than real Gemini providers so we don't
depend on `genai.Client` construction behaviour or network reachability.
"""
from __future__ import annotations

import app.generation as generation_module
from app.judge import Judge


class _SentinelProvider:
    """Identity-only stand-in — only `is`-comparisons matter to these tests."""


def test_get_provider_returns_singleton(monkeypatch):
    """A second call to `_get_provider()` must return the first call's
    instance, and the factory must run exactly once. Without this, the fix
    silently regresses to per-call construction."""
    monkeypatch.setattr(generation_module, "_provider", None)

    constructed: list[_SentinelProvider] = []

    def fake_factory() -> _SentinelProvider:
        p = _SentinelProvider()
        constructed.append(p)
        return p

    monkeypatch.setattr(generation_module, "get_provider", fake_factory)

    p1 = generation_module._get_provider()
    p2 = generation_module._get_provider()

    assert p1 is p2, "Expected the same provider instance across calls"
    assert len(constructed) == 1, "Factory should be invoked exactly once"


def test_get_judge_uses_shared_provider(monkeypatch):
    """The Judge singleton must hold the same provider instance returned by
    `_get_provider()`. If the judge constructed its own (or fell through to a
    bare `get_provider()`), we'd have two provider instances per process —
    exactly the waste P2.1 closed."""
    monkeypatch.setattr(generation_module, "_provider", None)
    monkeypatch.setattr(generation_module, "_judge", None)

    sentinel = _SentinelProvider()
    monkeypatch.setattr(generation_module, "get_provider", lambda: sentinel)

    provider = generation_module._get_provider()
    judge = generation_module._get_judge()

    assert isinstance(judge, Judge), "Expected the real Judge wrapper"
    # Judge stores the provider it was constructed with in `self._provider`
    # (see app/judge.py:Judge.__init__).
    assert judge._provider is provider, "Judge must share the singleton provider"
    assert judge._provider is sentinel, "Both should be the sentinel object"


def test_get_provider_factory_called_once_across_provider_and_judge(monkeypatch):
    """Sanity check that wiring `_get_judge()` through `_get_provider()`
    doesn't accidentally construct two providers — even if a future refactor
    reorders the lazy-init chain."""
    monkeypatch.setattr(generation_module, "_provider", None)
    monkeypatch.setattr(generation_module, "_judge", None)

    factory_calls = 0

    def counting_factory() -> _SentinelProvider:
        nonlocal factory_calls
        factory_calls += 1
        return _SentinelProvider()

    monkeypatch.setattr(generation_module, "get_provider", counting_factory)

    generation_module._get_provider()
    generation_module._get_judge()
    generation_module._get_provider()
    generation_module._get_judge()

    assert factory_calls == 1, (
        f"Expected get_provider() factory to be called exactly once across "
        f"both singletons; got {factory_calls}"
    )
