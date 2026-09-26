"""`llm_gateway/registry.py` — §6/§33/§34 : chargement du Model Registry
DEPUIS des settings (`.env`), format `"provider:model"`, jamais un nom de
modèle codé en dur dans un node métier."""

from __future__ import annotations

from types import SimpleNamespace

from ladini.graphs.agents.market_coach.llm_gateway.registry import (
    ModelRegistry,
    validate_config,
)
from ladini.graphs.agents.market_coach.llm_gateway.types import LLMProfile


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        LLM_FAST_PRIMARY="bedrock_gateway:qwen.qwen3-32b",
        LLM_FAST_FALLBACK_1="groq:llama-3.1-8b-instant",
        LLM_FAST_FALLBACK_2="",
        LLM_REASONING_PRIMARY="bedrock_gateway:deepseek.v3.2",
        LLM_REASONING_FALLBACK_1="bedrock_gateway:openai.gpt-oss-120b",
        LLM_REASONING_FALLBACK_2="groq:llama-3.3-70b-versatile",
        LLM_INTERPRETER_PRIMARY="groq:openai/gpt-oss-20b",
        LLM_INTERPRETER_FALLBACK_1="bedrock_gateway:qwen.qwen3-32b",
        LLM_INTERPRETER_FALLBACK_2="",
        OPENAI_BASE_URL="https://bedrock-gateway.internal.example/v1",
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


class TestValidateConfig:
    """(2026-09-26, audit LLM_GATEWAY_EXHAUSTED, §15) : détection SANS appel
    réseau des configurations manifestement invalides — voir
    `availability.py`, dont le commentaire documente déjà que l'absence
    d'`OPENAI_BASE_URL` est une mauvaise CIBLE (API OpenAI publique) jamais
    détectée comme CREDENTIAL_MISSING."""

    def test_valid_configuration_has_no_issues(self):
        assert validate_config(_settings()) == []

    def test_bedrock_gateway_candidate_without_openai_base_url_is_flagged(self):
        issues = validate_config(_settings(OPENAI_BASE_URL=""))
        assert any("OPENAI_BASE_URL" in issue and "vide" in issue for issue in issues)

    def test_bedrock_gateway_candidate_pointing_at_public_openai_is_flagged(self):
        issues = validate_config(_settings(OPENAI_BASE_URL="https://api.openai.com/v1"))
        assert any("api.openai.com" in issue for issue in issues)

    def test_a_profile_with_no_candidates_is_flagged(self):
        issues = validate_config(
            _settings(
                LLM_FAST_PRIMARY="", LLM_FAST_FALLBACK_1="", LLM_FAST_FALLBACK_2=""
            )
        )
        assert any("aucun candidat configuré" in issue for issue in issues)

    def test_a_duplicated_candidate_within_one_profile_is_flagged(self):
        issues = validate_config(
            _settings(
                LLM_REASONING_PRIMARY="bedrock_gateway:deepseek.v3.2",
                LLM_REASONING_FALLBACK_1="bedrock_gateway:deepseek.v3.2",
                LLM_REASONING_FALLBACK_2="",
            )
        )
        assert any("dupliqué" in issue for issue in issues)

    def test_registry_construction_logs_but_never_raises_on_invalid_config(self):
        """§15 : visibilité immédiate dans les logs de démarrage, jamais un
        crash — `ModelRegistry()` est instancié dans trop de contextes
        (tests, scripts) pour qu'un `raise` ici soit sûr sans revue dédiée."""
        ModelRegistry(_settings(OPENAI_BASE_URL=""))  # ne doit pas lever
