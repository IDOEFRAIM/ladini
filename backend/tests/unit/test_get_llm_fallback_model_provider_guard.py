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

from ladini.core.get_llm import _GroqAdapter, _fallback_model_for


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
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen.qwen3-32b")
        assert _fallback_model_for("openai/gpt-oss-120b") is None

    def test_slash_notation_groq_model_is_still_a_valid_fallback(self, monkeypatch):
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen/qwen3.6-27b")
        assert _fallback_model_for("openai/gpt-oss-120b") == "qwen/qwen3.6-27b"

    def test_same_model_requested_yields_no_fallback(self, monkeypatch):
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen/qwen3.6-27b")
        assert _fallback_model_for("qwen/qwen3.6-27b") is None

    def test_empty_configured_fallback_yields_none(self, monkeypatch):
        from ladini.core.settings import settings

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
        from ladini.core.settings import settings

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
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen/qwen3.6-27b")

        client = _FakeClient([_RateLimitError(), _Completion("La vraie réponse.")])
        adapter = _GroqAdapter(client)

        out = adapter.chat.completions.create(model="openai/gpt-oss-120b", messages=[])

        assert out.choices[0].message.content == "La vraie réponse."
        assert client.chat.completions.calls == [
            "openai/gpt-oss-120b",
            "qwen/qwen3.6-27b",
        ]


class TestGroqAdapterNestedFallbackIsScopedToRealGroq:
    """(2026-09-26, audit LLM_GATEWAY_EXHAUSTED) : `_GroqAdapter` est
    PARTAGÉ entre le vrai SDK Groq et la passerelle `bedrock_gateway`
    (protocole OpenAI identique — voir `core/get_llm.py::get_llm` et
    `llm_gateway/gateway.py::_default_client_for_provider`). Le repli
    legacy `_fallback_model_for` (`settings.LLM_MODEL`, un ID Groq notation
    slash) n'a de sens QUE contre le vrai Groq — envoyé à la passerelle
    Bedrock, il échouerait systématiquement (mauvaise notation de modèle)
    et masquerait le VRAI throttle/429 Bedrock initial derrière un faux 404
    "model not found" trompeur pour la classification d'erreur du LLM
    Gateway. `provider="bedrock_gateway"` désactive entièrement ce repli."""

    def test_bedrock_gateway_labelled_adapter_never_attempts_the_groq_fallback(
        self, monkeypatch
    ):
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen/qwen3-32b")

        client = _FakeClient([_RateLimitError()])
        adapter = _GroqAdapter(client, provider="bedrock_gateway")

        with pytest.raises(_RateLimitError):
            adapter.chat.completions.create(model="qwen.qwen3-32b", messages=[])

        # Une SEULE tentative — jamais de repli cross-provider vers un ID
        # Groq envoyé à la passerelle Bedrock.
        assert client.chat.completions.calls == ["qwen.qwen3-32b"]

    def test_groq_labelled_adapter_keeps_the_existing_fallback_behavior(
        self, monkeypatch
    ):
        """Non-regression: le comportement par défaut (provider="groq",
        valeur implicite pour tout appelant existant) est inchangé."""
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "LLM_MODEL", "qwen/qwen3-32b")

        client = _FakeClient([_RateLimitError(), _Completion("La vraie réponse.")])
        adapter = _GroqAdapter(client, provider="groq")

        out = adapter.chat.completions.create(model="openai/gpt-oss-120b", messages=[])

        assert out.choices[0].message.content == "La vraie réponse."
        assert client.chat.completions.calls == [
            "openai/gpt-oss-120b",
            "qwen/qwen3-32b",
        ]


class TestLlmModelIsARealGroqCatalogId:
    """(2026-09-26, audit LLM_GATEWAY_EXHAUSTED, 3e incident du même type —
    voir settings.py) : `settings.LLM_MODEL` sert de repli automatique sur
    tout 429 Groq (`_fallback_model_for`) — une valeur inexistante côté Groq
    transforme silencieusement un simple rate-limit en panne totale de
    l'appel LLM (404 `model_not_found`). Verrouille la valeur corrigée
    contre le SDK `groq` vendored (source de vérité la plus proche
    disponible sans appel réseau live — voir
    `groq/resources/chat/completions.py`) : un futur changement qui
    réintroduit un ID non reconnu par le SDK échoue ici plutôt qu'en
    production."""

    def test_default_llm_model_is_a_literal_the_groq_sdk_recognizes(self):
        import typing

        from groq.types.chat.completion_create_params import CompletionCreateParams

        from ladini.core.settings import Settings

        default_model = Settings.model_fields["LLM_MODEL"].default

        def _literals_in(tp: object) -> set[str]:
            """Parcours récursif — le paramètre `model` du SDK est
            `Required[Union[str, Literal[...]]]` : jamais une liste
            réinventée ici, seulement ce que le SDK vendored déclare
            lui-même (autocomplete du catalogue Groq au moment du release)."""
            out: set[str] = set()
            if typing.get_origin(tp) is typing.Literal:
                out.update(typing.get_args(tp))
                return out
            for arg in typing.get_args(tp):
                out |= _literals_in(arg)
            return out

        model_type = typing.get_type_hints(
            CompletionCreateParams, include_extras=True
        ).get("model")
        literal_values = _literals_in(model_type)

        assert literal_values, "aucune valeur Literal trouvée — le SDK groq a peut-être changé de forme"
        assert default_model in literal_values, (
            f"{default_model!r} n'apparaît pas dans les modèles connus du SDK "
            "groq vendored — vérifier console.groq.com/docs/models avant de "
            "changer cette valeur (voir settings.py, commentaire au-dessus "
            "de LLM_MODEL)."
        )
        assert default_model != "qwen/qwen3.6-27b", (
            "régression exacte de l'incident 2026-09-26 : cet ID n'existe "
            "pas sur Groq (ni 'qwen3.6' ni '27b' dans la gamme Qwen3 réelle)."
        )
