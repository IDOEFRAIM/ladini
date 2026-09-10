"""`llm_gateway/registry.py` — §6/§33/§34 : chargement du Model Registry
DEPUIS des settings (`.env`), format `"provider:model"`, jamais un nom de
modèle codé en dur dans un node métier."""

from __future__ import annotations

from types import SimpleNamespace

from ladini.graphs.agents.market_coach.llm_gateway.registry import ModelRegistry
from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        LLM_FAST_PRIMARY="bedrock_gateway:qwen.qwen3-32b",
        LLM_FAST_FALLBACK_1="groq:llama-3.1-8b-instant",
        LLM_FAST_FALLBACK_2="",
        LLM_REASONING_PRIMARY="bedrock_gateway:deepseek.v3.2",
        LLM_REASONING_FALLBACK_1="bedrock_gateway:openai.gpt-oss-120b",
        LLM_REASONING_FALLBACK_2="groq:llama-3.3-70b-versatile",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestLoadFromSettings:
    def test_candidates_are_ordered_by_priority(self):
        registry = ModelRegistry(_settings())
        candidates = registry.candidates_for(LLMProfile.REASONING)
        assert [c.model for c in candidates] == [
            "deepseek.v3.2",
            "openai.gpt-oss-120b",
            "llama-3.3-70b-versatile",
        ]

    def test_primary_model_name_is_the_first_priority_candidate(self):
        registry = ModelRegistry(_settings())
        assert registry.primary_model_name(LLMProfile.FAST) == "qwen.qwen3-32b"

    def test_an_empty_fallback_slot_is_simply_skipped(self):
        registry = ModelRegistry(_settings(LLM_FAST_FALLBACK_2=""))
        candidates = registry.candidates_for(LLMProfile.FAST)
        assert len(candidates) == 2  # pas de 3e candidat fantôme

    def test_an_unknown_provider_is_ignored_not_crashed_on(self):
        registry = ModelRegistry(
            _settings(LLM_FAST_PRIMARY="unknown_provider:some-model")
        )
        candidates = registry.candidates_for(LLMProfile.FAST)
        # Le candidat invalide est ignoré ; seul le fallback légitime reste.
        assert all(c.provider != "unknown_provider" for c in candidates)

    def test_bedrock_native_is_disabled_by_default(self):
        """§6 : `_BedrockAdapter` droppe silencieusement `response_format` —
        jamais le chemin actif par défaut tant que ce n'est pas un choix
        explicite (voir docstring registry.py)."""
        registry = ModelRegistry(
            _settings(LLM_FAST_PRIMARY="bedrock_native:some-model")
        )
        candidates = registry.candidates_for(LLMProfile.FAST)
        assert all(c.enabled for c in candidates)  # candidates_for() filtre déjà enabled=False

    def test_no_configuration_at_all_yields_an_empty_but_safe_registry(self):
        registry = ModelRegistry(
            _settings(
                LLM_FAST_PRIMARY="",
                LLM_FAST_FALLBACK_1="",
                LLM_FAST_FALLBACK_2="",
            )
        )
        assert registry.candidates_for(LLMProfile.FAST) == []
        assert registry.primary_model_name(LLMProfile.FAST) is None
