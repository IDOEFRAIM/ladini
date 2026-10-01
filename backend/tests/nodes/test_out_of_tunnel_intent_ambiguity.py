"""Étape 9A/9B — audit + détection d'ambiguïté hors tunnel (2026-10-01).

Incident-type :

    Aucun goal actif.
    User: "j'ai 90 L de miel"

Root cause auditée (voir le rapport) : le micro-prompt NEW_TASK
(`interpreter/new_task_contract.py`) forçait jusqu'ici le LLM à choisir
EXACTEMENT une intention du catalogue dès que `disposition=NEW_TASK` —
aucun moyen structurel d'exprimer "les faits sont clairs, l'action ne l'est
pas". Combiné à `_classify_nominal_action` (`nodes/cognitive.py`), qui
désactive la désambiguïsation lexicale existante dès que le LLM rapporte une
confiance ≥ 0.85 (quasi systématique pour un schéma à choix unique forcé),
"j'ai 90 L de miel" pouvait atterrir sur UNE intention arbitraire
(STOCK_REGISTER_HARVEST ou SALES_PUBLISH_PRODUCT) sans jamais que
l'ambiguïté réelle ne soit questionnée.

Ce fichier verrouille le nouveau mécanisme : `NewTaskDisposition.AMBIGUOUS`
(+ `candidate_goals`) et le nouveau garde de `cognitive_guard` qui construit
la clarification ciblée SANS jamais verrouiller de goal ni créer de draft.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    SUBFLOW_OWNED_KINDS,
    InteractionKind,
)
from ladini.graphs.agents.market_coach.interpreter.new_task_contract import (
    NewTaskDisposition,
    NewTaskInterpretation,
    adapt_new_task_to_canonical,
)
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from tests.conftest import StubRuntime, make_state, run


def _no_goal_ambiguous_state(*, facts, candidate_goals, text="", confidence=0.6):
    return make_state(
        normalized_text=text,
        current_goal=None,
        interpreted_event="AMBIGUOUS",
        detected_intent="UNKNOWN",
        interpreter_confidence=confidence,
        expected_input="NONE",
        extracted_entities=facts,
        candidate_goals=candidate_goals,
    )


# =====================================================================
# A/B/C/D — j'ai 90 L de miel : AMBIGUOUS, faits conservés, aucun side-effect
# =====================================================================


class TestMielAmbiguousCore:
    def test_A_no_active_goal_resolves_to_ambiguous_clarification(self):
        state = _no_goal_ambiguous_state(
            facts={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
            text="j'ai 90 L de miel",
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == ConversationAction.ASK_INTENT_SELECTION
        assert result["final_response"]
        assert result["ag_ui_component"]["kwargs"]["buttons"]

    def test_B_facts_are_preserved_in_the_pending_interaction_target(self):
        state = _no_goal_ambiguous_state(
            facts={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
            text="j'ai 90 L de miel",
        )
        result = run(cognitive_guard(state, None))
        pending = result["pending_interaction"]
        assert pending["kind"] == InteractionKind.CLARIFY_INTENT.value
        facts = pending["target"]["facts"]
        assert facts["product"] == "miel"
        assert facts["quantity"] == 90.0
        assert facts["unit"] == "LITRE"
        assert set(pending["target"]["candidate_goals"]) == {
            "SALES_PUBLISH_PRODUCT",
            "STOCK_REGISTER_HARVEST",
        }

    def test_C_no_sales_draft_or_goal_is_created(self):
        state = _no_goal_ambiguous_state(
            facts={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
        )
        result = run(cognitive_guard(state, None))
        assert "current_goal" not in result
        assert "sales_publish_draft" not in result

    def test_D_no_transaction_payload_mutation(self):
        state = _no_goal_ambiguous_state(
            facts={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
        )
        result = run(cognitive_guard(state, None))
        assert "transaction_payload" not in result

    def test_clarify_intent_is_a_subflow_owned_kind(self):
        """Garantit que `choose_interpretation_route` n'essaiera jamais de
        router une réponse à CETTE clarification vers ACTIVE_SLOT — sa
        résolution appartient à un flow dédié (Étape 9C), pas au
        classifieur générique ENTER_FIELD."""
        assert InteractionKind.CLARIFY_INTENT in SUBFLOW_OWNED_KINDS


# =====================================================================
# E/F — RESOLVED : signal d'action explicite => adaptation NEW_TASK directe
# (la décision LLM elle-même n'est pas testable sans appel réel ; on
# verrouille ici que le contrat + l'adaptation ne produisent JAMAIS
# AMBIGUOUS pour une disposition NEW_TASK bien formée).
# =====================================================================


class TestResolvedNeverTriggersAmbiguousAdaptation:
    def test_E_explicit_sales_intent_adapts_to_resolved_new_task(self):
        decision = NewTaskInterpretation(
            disposition=NewTaskDisposition.NEW_TASK,
            intent="SALES_PUBLISH_PRODUCT",
            confidence=0.95,
            entities={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
        )
        out = adapt_new_task_to_canonical(
            decision,
            entities={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            validation_status=None,
            locked_goal=None,
            path="test",
        )
        assert out["interpreted_event"] == "NEW_TASK"
        assert out["detected_intent"] == "SALES_PUBLISH_PRODUCT"
        assert "candidate_goals" not in out

    def test_F_explicit_stock_intent_adapts_to_resolved_new_task(self):
        decision = NewTaskInterpretation(
            disposition=NewTaskDisposition.NEW_TASK,
            intent="STOCK_REGISTER_HARVEST",
            confidence=0.95,
            entities={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
        )
        out = adapt_new_task_to_canonical(
            decision,
            entities={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            validation_status=None,
            locked_goal=None,
            path="test",
        )
        assert out["interpreted_event"] == "NEW_TASK"
        assert out["detected_intent"] == "STOCK_REGISTER_HARVEST"


# =====================================================================
# G/H — autres déclarations ambiguës, sans produit en dur
# =====================================================================


class TestOtherAmbiguousDeclarations:
    @pytest.mark.parametrize(
        "facts",
        [
            {"product": "tomates", "quantity": 100.0, "unit": "KG"},
            {"product": "lait", "quantity": 600.0, "unit": "LITRE"},
            {"product": "poulets", "quantity": 40.0, "unit": "UNITE"},
        ],
    )
    def test_G_H_bare_declarations_resolve_to_ambiguous(self, facts):
        state = _no_goal_ambiguous_state(
            facts=facts,
            candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] == ConversationAction.ASK_INTENT_SELECTION
        assert result["pending_interaction"]["target"]["facts"] == facts


# =====================================================================
# I — message flou, faits insuffisants => UNKNOWN (comportement existant,
# non modifié par ce correctif — verrouillé ici pour preuve).
# =====================================================================


class TestUnknownStillUnknown:
    def test_I_vague_message_stays_unknown(self):
        decision = NewTaskInterpretation(
            disposition=NewTaskDisposition.UNKNOWN, confidence=0.0
        )
        out = adapt_new_task_to_canonical(
            decision, entities={}, validation_status=None, locked_goal=None, path="test"
        )
        assert out["interpreted_event"] == "UNKNOWN"
        assert out["detected_intent"] == "UNKNOWN"
        assert "candidate_goals" not in out


# =====================================================================
# Contrat Pydantic — garde-fous de forme (mandat §9/§16 implicite)
# =====================================================================


class TestContractGuards:
    def test_ambiguous_requires_at_least_two_candidates(self):
        with pytest.raises(ValidationError):
            NewTaskInterpretation(
                disposition=NewTaskDisposition.AMBIGUOUS,
                candidate_goals=["SALES_PUBLISH_PRODUCT"],
                confidence=0.5,
            )

    def test_ambiguous_forbids_an_intent_field(self):
        with pytest.raises(ValidationError):
            NewTaskInterpretation(
                disposition=NewTaskDisposition.AMBIGUOUS,
                intent="SALES_PUBLISH_PRODUCT",
                candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
                confidence=0.5,
            )

    def test_new_task_forbids_candidate_goals(self):
        with pytest.raises(ValidationError):
            NewTaskInterpretation(
                disposition=NewTaskDisposition.NEW_TASK,
                intent="SALES_PUBLISH_PRODUCT",
                candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
                confidence=0.9,
            )

    def test_unknown_forbids_candidate_goals_and_entities(self):
        with pytest.raises(ValidationError):
            NewTaskInterpretation(
                disposition=NewTaskDisposition.UNKNOWN,
                candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
                confidence=0.0,
            )

    def test_ambiguous_preserves_facts_unlike_unknown(self):
        """Spec §2 : FACTS != ACTION — AMBIGUOUS, contrairement à UNKNOWN/
        CONFIRM/REJECT/OUT_OF_SCOPE, autorise des entities non vides."""
        decision = NewTaskInterpretation(
            disposition=NewTaskDisposition.AMBIGUOUS,
            candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
            confidence=0.6,
            entities={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
        )
        assert decision.entities.product == "miel"


# =====================================================================
# Insufficient-candidates safety net (pas dans le mandat mais découlant de
# "pas de goal choisi au hasard" — garde-fou défensif, contrat déjà exclut
# ce cas mais `cognitive_guard` reste prudent si jamais moins de 2 candidats
# valides survivent à un filtrage futur).
# =====================================================================


class TestLLMUnavailableNeverAutoSelectsStock:
    """Mandat §15/§16.J : la sûreté ne doit jamais dépendre du LLM. Sans
    LLM, `_input_interpreter_impl` retombe sur son repli existant
    (`no_llm`) — `_degraded_fallback` est réservé au rôle BUYER (voir sa
    docstring), donc un producteur retombe sur UNKNOWN sûr. Verrouillé ici
    comme preuve que ce repli PRÉEXISTANT ne devient jamais, par aucun
    chemin, STOCK_REGISTER_HARVEST/SALES_PUBLISH_PRODUCT automatique — le
    critère explicite du mandat ("AMBIGUOUS ou safe UNKNOWN, jamais STOCK
    automatique") est déjà satisfait par ce comportement existant, non
    modifié par cette PR."""

    def test_J_structured_declaration_without_llm_never_becomes_stock(self):
        interp = make_input_interpreter("PRODUCER")
        rt = StubRuntime(llm=None)
        state = make_state(
            normalized_text="j'ai 90 L de miel",
            current_goal=None,
            expected_input="NONE",
            user_role="PRODUCER",
        )
        result = run(interp(state, rt))
        assert result["detected_intent"] != "STOCK_REGISTER_HARVEST"
        assert result["detected_intent"] != "SALES_PUBLISH_PRODUCT"
        assert result["interpreted_event"] in ("UNKNOWN", "AMBIGUOUS")


class TestInsufficientCandidatesFallsBackSafely:
    def test_fewer_than_two_candidate_goals_never_picks_one_arbitrarily(self):
        state = _no_goal_ambiguous_state(
            facts={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            candidate_goals=["SALES_PUBLISH_PRODUCT"],
        )
        result = run(cognitive_guard(state, None))
        assert result["interpreted_event"] == "UNKNOWN"
        assert result["detected_intent"] == "UNKNOWN"
        assert "current_goal" not in result


# =====================================================================
# §18 — non-régression tunnel actif (Étape 7) : MÊME texte, contexte
# différent => classification différente.
# =====================================================================


class TestActiveTunnelNonRegression:
    def test_same_text_in_active_tunnel_stays_answer_not_ambiguous(self):
        """"j'ai 90 L de miel" pendant un tunnel SALES_PUBLISH_PRODUCT actif
        (expected=QUANTITY) doit rester un ANSWER du tunnel — jamais
        AMBIGUOUS. Ce test simule le cas où l'interpréteur aurait, par
        erreur, émis AMBIGUOUS alors qu'un goal est ACTIF : le garde Étape
        9A/9B l'ignore explicitement (`not current_goal`), laissant le tour
        retomber sur la logique Étape 7/nominale existante."""
        state = make_state(
            normalized_text="j'ai 90 L de miel",
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="AMBIGUOUS",
            detected_intent="UNKNOWN",
            interpreter_confidence=0.6,
            expected_input="QUANTITY",
            extracted_entities={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
            candidate_goals=["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"],
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] != ConversationAction.ASK_INTENT_SELECTION
        assert result.get("pending_interaction", {}).get("kind") != InteractionKind.CLARIFY_INTENT.value


# =====================================================================
# §19 — non-régression intents explicites : ces messages restent directs,
# cette PR ne transforme pas tout message hors tunnel en clarification.
# =====================================================================


class TestExplicitIntentsStayDirect:
    def test_explicit_new_task_intent_is_unaffected(self):
        """Un NEW_TASK normal (disposition NEW_TASK, pas AMBIGUOUS) ne passe
        JAMAIS par le nouveau garde — celui-ci ne regarde que
        `event == "AMBIGUOUS"`."""
        state = make_state(
            current_goal=None,
            interpreted_event="NEW_TASK",
            detected_intent="SALES_PUBLISH_PRODUCT",
            interpreter_confidence=0.95,
            expected_input="NONE",
            extracted_entities={"product": "miel", "quantity": 90.0, "unit": "LITRE"},
        )
        result = run(cognitive_guard(state, None))
        assert result["cognitive_decision"]["action"] != ConversationAction.ASK_INTENT_SELECTION
        assert "pending_interaction" not in result or (
            result["pending_interaction"].get("kind") != InteractionKind.CLARIFY_INTENT.value
        )
