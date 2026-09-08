"""`core/get_llm.py::_fallback_model_for` — incident réel (2026-09-07) :
`settings.LLM_MODEL` (réglage legacy, partagé avec la config multi-provider
du LLM Gateway — voir `docs/LLM_GATEWAY_FAILURE_RECOVERY_2026-09-05.md`) a
fini par contenir un ID au format Bedrock ("qwen.qwen3-32b", notation par
points) en production. Le repli automatique sur rate-limit Groq
(`GROQ_RATE_LIMIT_FALLBACK`, `_GroqAdapter.create`) tentait alors ce modèle
directement sur le client Groq, qui ne le connaît pas (404
`model_not_found`) — cassant tout appel LLM du tour (onboarding,
interprétation) dès qu'un simple 429 survenait sur le modèle principal.

Un ID contenant un point mais aucun slash est structurellement un modèle
Bedrock, jamais un modèle Groq valide (les ID Groq sont toujours de la
forme `provider/model`, ex: "qwen/qwen3.6-27b", "openai/gpt-oss-120b") —
`_fallback_model_for` doit refuser ce repli plutôt que de le tenter en
aveugle."""
from __future__ import annotations

import pytest

from agriconnect.core.get_llm import _GroqAdapter, _fallback_model_for


class _RateLimitError(Exception):
    """Stand-in for groq.RateLimitError — avoids a hard dependency on the
    real `groq` package being importable in the test environment (see
    `_is_rate_limit_error`'s own status_code=429 fallback path)."""

    def __init__(self):
        super().__init__("429 rate limit")
        self.status_code = 429


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs.get("model"))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeChat:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)


class _FakeClient:
    def __init__(self, responses):
        self.chat = _FakeChat(responses)


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = None


class TestFallbackModelForRejectsBedrockStyleIds:
    def test_dot_notation_without_slash_is_rejected(self, monkeypatch):
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen.qwen3-32b")
        assert _fallback_model_for("openai/gpt-oss-120b") is None

    def test_slash_notation_groq_model_is_still_a_valid_fallback(self, monkeypatch):
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen/qwen3.6-27b")
        assert _fallback_model_for("openai/gpt-oss-120b") == "qwen/qwen3.6-27b"

    def test_same_model_requested_yields_no_fallback(self, monkeypatch):
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen/qwen3.6-27b")
        assert _fallback_model_for("qwen/qwen3.6-27b") is None

    def test_empty_configured_fallback_yields_none(self, monkeypatch):
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "")
        assert _fallback_model_for("openai/gpt-oss-120b") is None


class TestGroqAdapterNeverRetriesWithABedrockStyleModel:
    def test_rate_limit_with_bedrock_style_fallback_raises_the_original_error(
        self, monkeypatch
    ):
        """The exact incident: a 429 on the primary model must never be
        followed by a doomed retry against a Bedrock-only model id — the
        original RateLimitError must propagate so the outer LLM Gateway's
        own candidate-fallback chain (a DIFFERENT, correctly-scoped
        mechanism) takes over instead."""
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen.qwen3-32b")

        client = _FakeClient([_RateLimitError()])
        adapter = _GroqAdapter(client)

        with pytest.raises(_RateLimitError):
            adapter.chat.completions.create(model="openai/gpt-oss-120b", messages=[])

        # Only the ONE doomed call — no second attempt against the invalid
        # Bedrock-style model id.
        assert client.chat.completions.calls == ["openai/gpt-oss-120b"]

    def test_rate_limit_with_a_valid_groq_fallback_still_recovers(self, monkeypatch):
        """Non-regression: the legitimate same-provider fallback (a real
        Groq model, separate TPD quota) must still work exactly as before."""
        from agriconnect.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen/qwen3.6-27b")

        client = _FakeClient([_RateLimitError(), _Completion("La vraie réponse.")])
        adapter = _GroqAdapter(client)

        out = adapter.chat.completions.create(model="openai/gpt-oss-120b", messages=[])

        assert out.choices[0].message.content == "La vraie réponse."
        assert client.chat.completions.calls == [
            "openai/gpt-oss-120b",
            "qwen/qwen3.6-27b",
        ]
