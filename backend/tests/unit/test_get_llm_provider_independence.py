"""`core/get_llm.py::get_groq_sdk` — indépendance vis-à-vis de `LLM_PROVIDER`
(incident réel 2026-09-05, voir le commentaire d'incident dans
`get_groq_sdk`). `LLM_PROVIDER` gouverne UNIQUEMENT le client LEGACY unique
construit par `get_llm()` — jamais un candidat individuel du LLM Gateway
(`llm_gateway/gateway.py::_default_client_for_provider`), qui appelle
`get_groq_sdk()` directement pour tout candidat `groq:*`, quel que soit
`LLM_PROVIDER`."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# `import agriconnect.core.get_llm as get_llm_mod` est peu fiable : le nom
# `get_llm` est à la fois le module ET une fonction qu'il définit, et
# `agriconnect.core` réexporte cette dernière — voir le même commentaire
# dans `tests/unit/test_get_llm_circuit_breaker.py`. `importlib` seul donne
# le vrai module.
_MOD = importlib.import_module("agriconnect.core.get_llm")


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Les clients construits par `core/get_llm.py` sont mis en cache
    module-level — sans reset, un test polluerait les suivants (même
    pattern que les autres tests de ce module)."""
    _MOD._GROQ_SDK_SINGLETON = None
    _MOD._LLM_SINGLETON = None
    _MOD._OPENAI_COMPATIBLE_SDK_SINGLETON = None
    yield
    _MOD._GROQ_SDK_SINGLETON = None
    _MOD._LLM_SINGLETON = None
    _MOD._OPENAI_COMPATIBLE_SDK_SINGLETON = None


class TestGroqSdkIgnoresLlmProviderSetting:
    @pytest.mark.parametrize("llm_provider", ["bedrock", "bedrock_gateway", "azure", ""])
    def test_builds_successfully_regardless_of_llm_provider_when_credential_present(
        self, llm_provider
    ):
        fake_settings = SimpleNamespace(
            MOCK_EXTERNAL_APIS=False,
            LLM_PROVIDER=llm_provider,
            llm_api_key="sk-real-groq-key",
        )
        with patch("agriconnect.core.settings.settings", fake_settings):
            with patch("groq.Groq") as mock_groq_cls:
                mock_groq_cls.return_value = object()
                client = _MOD.get_groq_sdk(force_refresh=True)

        assert client is not None
        mock_groq_cls.assert_called_once()

    def test_still_fails_fast_on_a_genuinely_missing_credential(self):
        """Le seul vrai motif de refus reste l'identifiant absent — pas
        `LLM_PROVIDER` (non-régression : ce garde ne doit pas être
        supprimé, seulement sa condition erronée)."""
        fake_settings = SimpleNamespace(
            MOCK_EXTERNAL_APIS=False,
            LLM_PROVIDER="groq",
            llm_api_key="",
        )
        with patch("agriconnect.core.settings.settings", fake_settings):
            with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
                _MOD.get_groq_sdk(force_refresh=True)


class TestBedrockFallbackClientNowActuallyWorks:
    """Second appelant touché par le même garde (silencieux, avalé par un
    `except Exception` local) : `get_llm()` en branche `LLM_PROVIDER=bedrock`
    construit un `fallback_client = get_groq_sdk()` pour son propre repli
    inter-provider — inopérant tant que le garde existait."""

    def test_bedrock_branch_builds_a_real_groq_fallback_client_when_a_key_is_present(self):
        fake_settings = SimpleNamespace(
            MOCK_EXTERNAL_APIS=False,
            LLM_PROVIDER="bedrock",
            OPENAI_BASE_URL="https://bedrock-gateway.example/v1",
            OPENAI_API_KEY="sk-gateway-key",
            llm_api_key="sk-real-groq-key",
        )
        with patch("agriconnect.core.settings.settings", fake_settings):
            with patch("openai.OpenAI") as mock_openai_cls, patch(
                "groq.Groq"
            ) as mock_groq_cls:
                mock_openai_cls.return_value = object()
                mock_groq_cls.return_value = object()
                adapter = _MOD.get_llm()

        assert adapter is not None
        # Le fallback_client interne n'est PAS None — la construction de
        # Groq() a bien été tentée (avant le fix, `get_groq_sdk()` levait
        # et l'exception était avalée par le `except Exception` local de
        # `get_llm()`, laissant `fallback_client=None` silencieusement).
        mock_groq_cls.assert_called_once()
