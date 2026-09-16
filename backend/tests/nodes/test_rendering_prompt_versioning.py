"""Chantier "State Router + micro-prompts" — Incrément E (2026-09-13) :
ASK / CLARIFICATION / DEVIATION rendering.

## Audit préalable (voir le rapport final pour le détail complet)

Contrairement aux routes SELECTION/ACTIVE_SLOT/STRUCTURED_ACTION, les 3
renderers concernés ici (`nodes/rendering/ask.py::generate_llm_question`,
`nodes/clarification.py::clarification_node`, `utils.py::
llm_deviation_reply`) étaient DÉJÀ, avant cet incrément, des micro-prompts
minimaux : aucun catalogue des ~41 intentions, aucun schéma JSON, aucun ID
technique, `max_tokens`/`temperature` déjà dans les plages cibles de la
spec (60-150 / 0.2-0.4). Ce fichier verrouille cet état ET ajoute ce qui
manquait réellement : versioning explicite + traçabilité Langfuse
(`extra_metadata`), et des gardes structurelles anti-régression.

Note technique : `llm_gateway.resolve_gateway(mc_runtime)` retombe sur
`LegacyOverrideGateway` quand `mc_runtime` n'expose qu'un `.llm` nu (pattern
`_StubLLM` des autres suites) — cette passerelle de compat DROP
explicitement `extra_metadata` (voir sa docstring : "SANS EFFET ici").
Pour vérifier que la télémétrie Langfuse atteint bien l'appel réel, ce
fichier fournit donc directement un `.llm_gateway` (jamais juste `.llm`) —
c'est la branche prioritaire de `resolve_gateway`, utilisée en production."""
from __future__ import annotations

from typing import Any, Dict, List

from ladini.graphs.agents.market_coach.interpreter.intent import INTENT_CONFIG
import ladini.graphs.agents.market_coach.nodes.clarification as clarification_module
import ladini.graphs.agents.market_coach.nodes.rendering.ask as ask_module
import ladini.graphs.agents.market_coach.utils as utils_module
from tests.conftest import run


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _CapturingGateway:
    """Fausse Gateway complète (pas `LegacyOverrideGateway`) : capture tous
    les kwargs réellement passés à `.complete(...)`, `extra_metadata`
    compris — c'est ce que fait la vraie `LLMGateway` avant de les remonter
    à Langfuse (voir `llm_gateway/gateway.py`)."""

    def __init__(self, reply: str) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._reply = reply

    async def complete(self, **kwargs: Any):
        self.calls.append(kwargs)
        return _Completion(self._reply)

    def primary_model_name(self, profile: Any) -> str:
        return "test-model"


def _runtime(reply: str) -> Any:
    gateway = _CapturingGateway(reply)
    rt = type("RT", (), {"llm": object(), "llm_gateway": gateway})()
    return rt, gateway


# =====================================================================
# A. PROMPT VERSIONING (spec §19)
# =====================================================================


class TestPromptVersionsAreDistinctAndExplicit:
    def test_ask_prompt_version(self):
        assert ask_module.ASK_PROMPT_VERSION == "ask_v1"

    def test_clarification_prompt_version(self):
        assert clarification_module.CLARIFICATION_PROMPT_VERSION == "clarification_v1"

    def test_deviation_reply_prompt_version(self):
        assert utils_module.DEVIATION_REPLY_PROMPT_VERSION == "deviation_reply_v1"

    def test_all_three_versions_are_distinct(self):
        versions = {
            ask_module.ASK_PROMPT_VERSION,
            clarification_module.CLARIFICATION_PROMPT_VERSION,
            utils_module.DEVIATION_REPLY_PROMPT_VERSION,
        }
        assert len(versions) == 3


# =====================================================================
# B. LANGFUSE — extra_metadata atteint bien l'appel gateway (spec §21)
# =====================================================================


class TestLangfuseMetadataReachesTheGatewayCall:
    def test_ask_carries_prompt_family_and_version(self):
        rt, gateway = _runtime("Quelle quantité de maïs souhaitez-vous vendre ?")
        run(
            ask_module.generate_llm_question(
                rt, "SALES_PUBLISH_PRODUCT", "quantity", "la quantité",
                {"product": "mais"},
            )
        )
        assert len(gateway.calls) == 1
        meta = gateway.calls[0]["extra_metadata"]
        assert meta["prompt_family"] == "ask"
        assert meta["prompt_version"] == "ask_v1"

    def test_clarification_carries_prompt_family_and_version(self):
        rt, gateway = _runtime("Je peux vous aider à vendre ou acheter !")
        state = {
            "cognitive_decision": {"action": "CLARIFY"},
            "user_role": "BUYER",
            "normalized_text": "bof",
        }
        run(clarification_module.clarification_node(state, rt))
        assert len(gateway.calls) == 1
        meta = gateway.calls[0]["extra_metadata"]
        assert meta["prompt_family"] == "clarification"
        assert meta["prompt_version"] == "clarification_v1"

    def test_deviation_reply_carries_prompt_family_and_version(self):
        rt, gateway = _runtime("Je comprends, laissez-moi vous aider.")
        run(
            utils_module.llm_deviation_reply(
                rt, "c'est vraiment nécessaire ?", "confirmer votre commande"
            )
        )
        assert len(gateway.calls) == 1
        meta = gateway.calls[0]["extra_metadata"]
        assert meta["prompt_family"] == "deviation_reply"
        assert meta["prompt_version"] == "deviation_reply_v1"


# =====================================================================
# C. MAX_TOKENS EXPLICITE (spec §16) — jamais un appel illimité
# =====================================================================


class TestMaxTokensAreExplicit:
    def test_ask_has_an_explicit_max_tokens(self):
        rt, gateway = _runtime("Une question ?")
        run(
            ask_module.generate_llm_question(
                rt, "SALES_PUBLISH_PRODUCT", "quantity", "la quantité", {}
            )
        )
        assert gateway.calls[0]["max_tokens"] is not None
        assert 0 < gateway.calls[0]["max_tokens"] <= 150

    def test_clarification_has_an_explicit_max_tokens(self):
        rt, gateway = _runtime("Je peux vous aider.")
        state = {"cognitive_decision": {"action": "CLARIFY"}, "user_role": "BUYER"}
        run(clarification_module.clarification_node(state, rt))
        assert gateway.calls[0]["max_tokens"] is not None
        assert 0 < gateway.calls[0]["max_tokens"] <= 200

    def test_deviation_reply_has_an_explicit_max_tokens(self):
        rt, gateway = _runtime("Compris !")
        run(utils_module.llm_deviation_reply(rt, "hein ?", "confirmer"))
        assert gateway.calls[0]["max_tokens"] is not None
        assert 0 < gateway.calls[0]["max_tokens"] <= 200


# =====================================================================
# D. GARDE ANTI-CATALOGUE / ANTI-ID (spec §27)
# =====================================================================


class TestNoOrchestrationLeaksIntoRenderingPrompts:
    """Ces renderers ne doivent JAMAIS redevenir un second interpréteur —
    aucun catalogue d'intentions, aucun schéma JSON, aucun ID technique."""

    def test_ask_prompt_never_contains_the_intent_catalog_keys(self):
        rt, gateway = _runtime("Une question ?")
        run(
            ask_module.generate_llm_question(
                rt, "SALES_PUBLISH_PRODUCT", "quantity", "la quantité",
                {"product": "mais"},
            )
        )
        prompt = gateway.calls[0]["messages"][0]["content"]
        sample_intents = list(INTENT_CONFIG.keys())[:10]
        for intent_key in sample_intents:
            if intent_key == "SALES_PUBLISH_PRODUCT":
                continue  # légitimement présent : c'est le but courant
            assert intent_key not in prompt

    def test_ask_prompt_never_contains_a_json_schema_marker(self):
        rt, gateway = _runtime("Une question ?")
        run(
            ask_module.generate_llm_question(
                rt, "SALES_PUBLISH_PRODUCT", "quantity", "la quantité", {}
            )
        )
        prompt = gateway.calls[0]["messages"][0]["content"]
        for forbidden in ("agent_action", "producer_id", "pricing_tier_id", "interpreted_event"):
            assert forbidden not in prompt

    def test_clarification_prompt_never_contains_an_id_field(self):
        rt, gateway = _runtime("Je peux vous aider.")
        state = {"cognitive_decision": {"action": "CLARIFY"}, "user_role": "BUYER"}
        run(clarification_module.clarification_node(state, rt))
        prompt = gateway.calls[0]["messages"][0]["content"]
        for forbidden in ("agent_action", "producer_id", "pricing_tier_id", "selection_index"):
            assert forbidden not in prompt

    def test_deviation_reply_prompt_never_contains_an_id_field(self):
        rt, gateway = _runtime("Compris !")
        run(utils_module.llm_deviation_reply(rt, "hein ?", "confirmer votre commande"))
        prompt = gateway.calls[0]["messages"][0]["content"]
        for forbidden in ("agent_action", "producer_id", "pricing_tier_id", "selection_index"):
            assert forbidden not in prompt

    def test_ask_prompt_never_asks_for_a_json_response(self):
        # Les 3 prompts doivent produire du TEXTE, jamais demander un objet
        # JSON structuré — la classification est déjà faite en amont.
        rt, gateway = _runtime("Une question ?")
        run(
            ask_module.generate_llm_question(
                rt, "SALES_PUBLISH_PRODUCT", "quantity", "la quantité", {}
            )
        )
        prompt = gateway.calls[0]["messages"][0]["content"]
        assert "JSON" not in prompt


# =====================================================================
# E. SÉCURITÉ — fallback jamais vide, jamais un crash (spec §23)
# =====================================================================


# =====================================================================
# F. SCÉNARIO MÉTIER RÉEL — DEVIATION reply ne reclasse jamais (spec §26)
# =====================================================================


class TestDeviationReplyStaysCoherentWithAnAlreadyMadeDecision:
    """`llm_deviation_reply` est un RENDERER : le planner a déjà décidé que
    l'ancienne tâche est interrompue/suspendue — ce renderer ne doit produire
    qu'une phrase de transition cohérente avec cette décision, jamais
    re-décider si le changement de sujet est légitime."""

    def test_maize_to_eggs_scenario_produces_plain_text_not_a_reclassification(self):
        rt, gateway = _runtime(
            "D'accord, on met la publication du maïs de côté — parlons des œufs !"
        )
        result = run(
            utils_module.llm_deviation_reply(
                rt,
                "en fait laisse tomber le mais, je cherche des oeufs",
                "publier votre récolte de maïs (quantité, prix)",
            )
        )
        assert isinstance(result, str) and result.strip()
        assert "{" not in result and "}" not in result  # jamais de JSON visible
        prompt = gateway.calls[0]["messages"][0]["content"]
        # Le prompt transmet le CONTEXTE déjà tranché (tâche en cours), pas
        # une invite à reclassifier une intention.
        assert "publier votre récolte de maïs" in prompt
        for forbidden in ("agent_action", "disposition", "interpreted_event", "SALES_PUBLISH_PRODUCT"):
            assert forbidden not in prompt


class TestSafeFallbackOnInfrastructureFailure:
    class _BoomingGateway:
        async def complete(self, **kwargs: Any):
            raise RuntimeError("gateway down")

        def primary_model_name(self, profile: Any) -> str:
            return "test-model"

    def _booming_runtime(self) -> Any:
        return type("RT", (), {"llm": object(), "llm_gateway": self._BoomingGateway()})()

    def test_ask_falls_back_to_a_safe_string_on_llm_exception(self):
        rt = self._booming_runtime()
        result = run(
            ask_module.generate_llm_question(
                rt, "SALES_PUBLISH_PRODUCT", "quantity", "la quantité", {}
            )
        )
        assert isinstance(result, str) and result.strip()

    def test_clarification_falls_back_to_an_empty_patch_on_llm_exception(self):
        rt = self._booming_runtime()
        state = {"cognitive_decision": {"action": "CLARIFY"}, "user_role": "BUYER"}
        result = run(clarification_module.clarification_node(state, rt))
        assert result == {}  # le fallback générique CLARIFICATION prend le relais en amont

    def test_deviation_reply_returns_none_on_llm_exception(self):
        rt = self._booming_runtime()
        result = run(utils_module.llm_deviation_reply(rt, "hein ?", "confirmer"))
        assert result is None
