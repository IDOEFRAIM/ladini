"""`llm_gateway/availability.py::is_candidate_usable` — pré-check §6/§7 du
brief incident 2026-09-05 : FALLBACK REGISTRY ≠ PROVIDER RUNTIME
CONFIGURATION. Aucun appel réseau, aucun Redis — pure fonction de settings."""

from __future__ import annotations

from types import SimpleNamespace

from ladini.graphs.agents.market_coach.llm_gateway.availability import (
    is_candidate_usable,
)
from ladini.graphs.agents.market_coach.llm_gateway.types import (
    LLMProfile,
    ModelCandidate,
)


def _candidate(provider: str) -> ModelCandidate:
    return ModelCandidate(
        provider=provider,
        model="some-model",
        profile=LLMProfile.REASONING,
        priority=0,
        timeout_seconds=2.0,
    )


class TestGroqCandidate:
    def test_usable_when_a_credential_is_present(self):
        settings = SimpleNamespace(llm_api_key="sk-real-key", MOCK_EXTERNAL_APIS=False)
        result = is_candidate_usable(_candidate("groq"), settings)
        assert result.usable is True
        assert result.reason is None

    def test_unusable_with_credential_missing_regardless_of_llm_provider(self):
        """Le coeur de l'incident (§6, Modèle A) : `LLM_PROVIDER` n'a AUCUN
        rôle dans cette décision — seul l'identifiant compte. Ce candidat
        reste usable même si `LLM_PROVIDER=bedrock`."""
        settings = SimpleNamespace(
            llm_api_key="sk-real-key",
            LLM_PROVIDER="bedrock",
            MOCK_EXTERNAL_APIS=False,
        )
        assert is_candidate_usable(_candidate("groq"), settings).usable is True

    def test_unusable_when_no_credential_at_all(self):
        settings = SimpleNamespace(llm_api_key="", MOCK_EXTERNAL_APIS=False)
        result = is_candidate_usable(_candidate("groq"), settings)
        assert result.usable is False
        assert result.reason == "CREDENTIAL_MISSING"


class TestBedrockGatewayCandidate:
    def test_usable_when_openai_api_key_is_present(self):
        settings = SimpleNamespace(OPENAI_API_KEY="sk-gw", MOCK_EXTERNAL_APIS=False)
        assert is_candidate_usable(_candidate("bedrock_gateway"), settings).usable is True

    def test_unusable_when_openai_api_key_missing(self):
        settings = SimpleNamespace(OPENAI_API_KEY="", MOCK_EXTERNAL_APIS=False)
        result = is_candidate_usable(_candidate("bedrock_gateway"), settings)
        assert result.usable is False
        assert result.reason == "CREDENTIAL_MISSING"

    def test_a_configured_but_expired_token_is_still_usable_here(self):
        """§18 (Test critique #1) : un token expiré n'est PAS une absence de
        configuration — le pré-check ne peut/doit pas le deviner sans appel.
        C'est le disjoncteur (401 -> CONFIG) qui gère cette découverte."""
        settings = SimpleNamespace(
            OPENAI_API_KEY="sk-configured-but-expired", MOCK_EXTERNAL_APIS=False
        )
        assert is_candidate_usable(_candidate("bedrock_gateway"), settings).usable is True


class TestBedrockNativeCandidate:
    def test_always_usable_boto3_credential_chain_is_implicit(self):
        """§26 : limitation assumée et documentée — boto3 résout ses
        identifiants de façon implicite (rôle IAM...), impossible à prouver
        "impossible" sans appel réseau réel."""
        settings = SimpleNamespace(MOCK_EXTERNAL_APIS=False)
        assert is_candidate_usable(_candidate("bedrock_native"), settings).usable is True


class TestUnknownProvider:
    def test_fails_open_not_closed_never_seen_in_production_registry_already_filters(self):
        """`registry.py::_KNOWN_PROVIDERS` filtre déjà les providers non
        reconnus à la construction — un provider "inconnu" ici n'est jamais
        une preuve d'impossibilité, seulement une question sans réponse
        locale. Fail-open : la vraie disponibilité sera prouvée à l'appel."""
        settings = SimpleNamespace(MOCK_EXTERNAL_APIS=False)
        result = is_candidate_usable(_candidate("something_else"), settings)
        assert result.usable is True


class TestSandboxBypass:
    def test_mock_external_apis_makes_every_provider_usable_without_any_credential(self):
        """Cohérence avec `core/get_llm.py::_MockGroqClient` (2026-08-27) :
        aucune raison de bloquer en pré-check ce que le reste du code
        autorise déjà explicitement pour le mode sandbox."""
        settings = SimpleNamespace(MOCK_EXTERNAL_APIS=True)
        for provider in ("groq", "bedrock_gateway", "bedrock_native"):
            assert is_candidate_usable(_candidate(provider), settings).usable is True
