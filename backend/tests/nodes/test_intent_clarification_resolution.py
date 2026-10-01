"""Étape 9C — résolution après clarification d'intention (2026-10-01).

Scénario exact du mandat :

    User: "j'ai 90 L de miel"
    Ladini: "Souhaitez-vous les mettre en vente ou les enregistrer dans
             votre stock ?"               (pending_interaction = CLARIFY_INTENT,
                                            facts={product:miel,quantity:90,unit:LITRE},
                                            candidate_goals=[SALES_PUBLISH_PRODUCT,
                                                              STOCK_REGISTER_HARVEST])
    User: "vendre"

Attendu : goal=SALES_PUBLISH_PRODUCT, product=miel, quantity=90 L conservés,
`pending_interaction` CONSOMMÉ, prochaine question = PRICE — sans jamais
redemander produit/quantité/unité. Preuve complète (faits + pending_
interaction + validator réel) : voir le replay E2E, `tests/integration/
test_intent_clarification_resolution_e2e.py`.

Root cause auditée avant code (voir le rapport) : rien ne résolvait
`InteractionKind.CLARIFY_INTENT` — une réponse comme "vendre" repartait dans
le classifieur NEW_TASK normal, hors contexte, sans jamais consulter les
faits déjà préservés ni les `candidate_goals`.

`nodes/cognitive.py::_resolve_intent_clarification` ferme ce trou SANS
jamais verrouiller `current_goal` lui-même — contrainte architecturale
vérifiée par `tests/architecture/test_canonical_goal_api.py`
(`cognitive_guard` ne DÉCIDE jamais du goal, seul `goal_planner` en est le
propriétaire déclaré). Une résolution réussie pose donc exactement ce qu'un
tour "je veux vendre 90 L de miel" tapé directement aurait posé
(`interpreted_event=NEW_TASK`/`detected_intent=<goal choisi>`/
`extracted_entities=<faits fusionnés>`) et route vers `goal_planner`
(`START_OR_PLAN_GOAL`, déjà câblé "to_planner") — jamais un bypass de l'API
canonique du goal ni un chemin spécial "if SALES: ask PRICE" codé en dur.
Ce fichier verrouille donc le contrat DIRECT de `cognitive_guard`
(sélection du goal, fusion des faits, annulation, reclarification,
breakout) — la preuve du résultat final (goal verrouillé, prochaine
question) vit dans le replay E2E."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    set_pending_interaction,
)
from ladini.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from tests.conftest import run

_FACTS = {"product": "miel", "quantity": 90.0, "unit": "LITRE"}
_CANDIDATES = ["SALES_PUBLISH_PRODUCT", "STOCK_REGISTER_HARVEST"]


def _pending_clarification_state(
    *,
    text: str,
    event: str = "UNKNOWN",
    intent: str = "UNKNOWN",
    extracted_entities=None,
    facts=None,
    candidate_goals=None,
):
    state = {
        "normalized_text": text,
        "user_query": text,
        "current_goal": None,
        "working_memory": {},
        "transaction_payload": {},
        "interpreted_event": event,
        "detected_intent": intent,
        "interpreter_confidence": 0.9 if event == "NEW_TASK" else 0.0,
        "extracted_entities": extracted_entities or {},
        "user_phone": "+22670000099",
        "user_role": "PRODUCER",
    }
    state.update(
        set_pending_interaction(
            InteractionKind.CLARIFY_INTENT,
            context_ref="out_of_tunnel_intent_ambiguity",
            target={
                "facts": facts if facts is not None else dict(_FACTS),
                "candidate_goals": candidate_goals or list(_CANDIDATES),
            },
        )
    )
    return state


# =====================================================================
# A-D — formulations naturelles SALES/STOCK (lexicales, aucun LLM requis)
#
# `cognitive_guard` seul ne verrouille jamais `current_goal` (voir la
# docstring de module) : une résolution réussie se reconnaît à
# `interpreted_event=="NEW_TASK"` + `detected_intent==<goal choisi>` +
# `cognitive_decision.action==START_OR_PLAN_GOAL` (routage "to_planner",
# où `goal_planner` verrouille RÉELLEMENT le goal — prouvé par le replay
# E2E, pas retesté ici).
# =====================================================================


def _assert_resolves_to(result, goal: str, *, source: str = "lexical"):
    assert result["interpreted_event"] == "NEW_TASK"
    assert result["detected_intent"] == goal
    assert result["cognitive_decision"]["action"] == ConversationAction.START_OR_PLAN_GOAL
    assert result["cognitive_decision"]["selected_goal"] == goal
    assert result["cognitive_decision"]["resolution_source"] == source
    assert result["pending_interaction"] is None


class TestNaturalLexicalResolution:
    def test_A_vendre_resolves_sales(self):
        state = _pending_clarification_state(text="vendre")
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT")

    def test_B_je_veux_les_vendre_resolves_sales(self):
        state = _pending_clarification_state(text="je veux les vendre")
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT")

    def test_mets_les_en_vente_resolves_sales(self):
        state = _pending_clarification_state(text="mets-les en vente")
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT")

    def test_a_vendre_resolves_sales(self):
        state = _pending_clarification_state(text="à vendre")
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT")

    def test_C_stock_resolves_stock(self):
        state = _pending_clarification_state(text="stock")
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "STOCK_REGISTER_HARVEST")

    def test_D_je_veux_les_enregistrer_resolves_stock(self):
        state = _pending_clarification_state(text="je veux les enregistrer")
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "STOCK_REGISTER_HARVEST")

    def test_mets_les_dans_mon_stock_resolves_stock(self):
        state = _pending_clarification_state(text="mets-les dans mon stock")
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "STOCK_REGISTER_HARVEST")


# =====================================================================
# Faits fusionnés posés dans `extracted_entities` — c'est `memory_update`
# (en aval de `goal_planner`) qui les écrit ensuite dans
# `transaction_payload` ; vérifié ici au niveau du contrat direct de
# `cognitive_guard`, vérifié de bout en bout dans le replay E2E.
# =====================================================================


class TestMergedFactsArePassedAsExtractedEntities:
    def test_facts_are_carried_in_extracted_entities(self):
        state = _pending_clarification_state(text="vendre")
        result = run(cognitive_guard(state, None))
        assert result["extracted_entities"] == _FACTS


# =====================================================================
# E/F — réponse enrichie : résout l'intention ET corrige/enrichit les faits
# =====================================================================


class TestEnrichedResponseOverridesFacts:
    def test_E_vendre_seulement_50_l_overrides_quantity(self):
        state = _pending_clarification_state(
            text="je veux en vendre seulement 50 L",
            event="NEW_TASK",
            intent="SALES_PUBLISH_PRODUCT",
            extracted_entities={"quantity": 50.0, "unit": "LITRE"},
        )
        result = run(cognitive_guard(state, None))
        # Le texte porte lui-même le radical "vendre" -> résolution lexicale
        # (voir `_select_clarified_goal`), même si l'interpréteur a AUSSI
        # fourni une correction de quantité dans `extracted_entities`.
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT", source="lexical")
        entities = result["extracted_entities"]
        assert entities["product"] == "miel"
        assert entities["quantity"] == 50.0

    def test_F_en_fait_120_l_et_je_veux_les_vendre(self):
        state = _pending_clarification_state(
            text="en fait 120 L et je veux les vendre",
            event="NEW_TASK",
            intent="SALES_PUBLISH_PRODUCT",
            extracted_entities={"quantity": 120.0, "unit": "LITRE"},
        )
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT")
        assert result["extracted_entities"]["quantity"] == 120.0

    def test_price_given_in_the_same_reply_is_not_discarded(self):
        """Mandat §16 : "je veux les vendre à 700 FCFA par litre" ne doit
        jamais jeter le prix sous prétexte que c'était une réponse à une
        clarification."""
        state = _pending_clarification_state(
            text="je veux les vendre à 700 FCFA par litre",
            event="NEW_TASK",
            intent="SALES_PUBLISH_PRODUCT",
            extracted_entities={"price": 700.0, "price_unit": "LITRE"},
        )
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT")
        entities = result["extracted_entities"]
        assert entities["product"] == "miel"
        assert entities["quantity"] == 90.0
        assert entities["price"] == 700.0


# =====================================================================
# G — réponse trop ambiguë : ne choisit rien, reclarifie
# =====================================================================


class TestAmbiguousReplyNeverPicksAGoalArbitrarily:
    def test_G_oui_does_not_resolve_a_goal(self):
        state = _pending_clarification_state(text="oui", event="CONFIRM")
        result = run(cognitive_guard(state, None))
        assert "detected_intent" not in result or result.get("detected_intent") != "SALES_PUBLISH_PRODUCT"
        assert result["pending_interaction"]["kind"] == InteractionKind.CLARIFY_INTENT.value
        assert result["cognitive_decision"]["action"] == ConversationAction.ASK_INTENT_SELECTION

    def test_d_accord_does_not_resolve_a_goal(self):
        state = _pending_clarification_state(text="d'accord", event="CONFIRM")
        result = run(cognitive_guard(state, None))
        assert result["pending_interaction"]["kind"] == InteractionKind.CLARIFY_INTENT.value
        assert result["cognitive_decision"]["action"] == ConversationAction.ASK_INTENT_SELECTION


# =====================================================================
# H — annulation propre
# =====================================================================


class TestCancellationIsClean:
    def test_H_annuler_clears_pending_with_no_side_effect(self):
        state = _pending_clarification_state(text="annuler", event="REJECT")
        result = run(cognitive_guard(state, None))
        assert result["pending_interaction"] is None
        assert "current_goal" not in result
        assert "sales_publish_draft" not in result
        assert "transaction_payload" not in result
        assert result["cognitive_decision"]["action"] == ConversationAction.RESOLVE_INTENT_CLARIFICATION


# =====================================================================
# I — nouvelle tâche explicite pendant la clarification : breakout, pas de
# goal forcé parmi les candidats.
# =====================================================================


class TestBreakoutDuringClarification:
    def test_I_real_new_task_outside_candidates_is_not_forced_into_sales_or_stock(self):
        state = _pending_clarification_state(
            text="je veux consulter mes commandes",
            event="NEW_TASK",
            intent="SALES_LIST_ORDERS",
        )
        result = run(cognitive_guard(state, None))
        # Le pending CLARIFY_INTENT est effacé ; `detected_intent` n'est PAS
        # réécrit par ce garde (il reste tel que l'interpréteur l'a déjà
        # posé dans `state` — ni modifié ni consommé ici) — c'est la
        # classification NOMINALE de ce nœud (`_classify_nominal_action`,
        # inchangée) qui décide la suite, jamais ce garde-ci.
        assert result.get("pending_interaction") is None
        assert "detected_intent" not in result  # jamais réécrit par ce garde
        assert result["cognitive_decision"]["action"] not in (
            ConversationAction.RESOLVE_INTENT_CLARIFICATION,
            ConversationAction.ASK_INTENT_SELECTION,
        )
        assert "selected_goal" not in result["cognitive_decision"]


# =====================================================================
# §21 — conflit produit : ne colle jamais l'ancienne quantité sur le
# nouveau produit.
# =====================================================================


class TestProductConflictNeverCarriesOverTheOldQuantity:
    def test_new_product_never_inherits_the_old_products_quantity(self):
        state = _pending_clarification_state(
            text="je veux vendre le lait",
            event="NEW_TASK",
            intent="SALES_PUBLISH_PRODUCT",
            extracted_entities={"product": "lait"},
        )
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT")
        entities = result["extracted_entities"]
        assert entities["product"] == "lait"
        assert entities.get("quantity") != 90.0, (
            f"la quantité du miel (90 L) a contaminé le lait : {entities!r}"
        )
        assert "quantity" not in entities  # jamais devinée, redemandée en aval

    def test_new_product_with_its_own_quantity_uses_only_the_new_values(self):
        state = _pending_clarification_state(
            text="je veux vendre 30 L de lait",
            event="NEW_TASK",
            intent="SALES_PUBLISH_PRODUCT",
            extracted_entities={"product": "lait", "quantity": 30.0, "unit": "LITRE"},
        )
        result = run(cognitive_guard(state, None))
        entities = result["extracted_entities"]
        assert entities["product"] == "lait"
        assert entities["quantity"] == 30.0


# =====================================================================
# J/K — LLM indisponible : sûreté déterministe pour les réponses simples.
# =====================================================================


class TestDeterministicWithoutRelyingOnTheInterpreter:
    """`_select_clarified_goal` résout "vendre"/"stock" par radical lexical
    AVANT même de consulter `detected_intent` — fonctionne à l'identique
    que l'interpréteur ait réussi ou non (mandat §18/§19). Simulé ici en
    passant un `event`/`detected_intent` "UNKNOWN" (= ce qu'un LLM
    indisponible produirait), pour prouver que la résolution ne dépend
    QUE du texte et des `candidate_goals`."""

    def test_J_vendre_resolves_even_with_an_unknown_interpreter_result(self):
        state = _pending_clarification_state(
            text="vendre", event="UNKNOWN", intent="UNKNOWN"
        )
        result = run(cognitive_guard(state, None))
        _assert_resolves_to(result, "SALES_PUBLISH_PRODUCT")

    def test_K_oui_without_a_usable_interpreter_result_stays_a_clarification(self):
        state = _pending_clarification_state(
            text="oui", event="UNKNOWN", intent="UNKNOWN"
        )
        result = run(cognitive_guard(state, None))
        assert result["pending_interaction"]["kind"] == InteractionKind.CLARIFY_INTENT.value
        assert result["cognitive_decision"]["action"] == ConversationAction.ASK_INTENT_SELECTION


# =====================================================================
# §25 — no-side-effect avant résolution valide (reclarification/cancel).
# =====================================================================


class TestNoSideEffectBeforeValidResolution:
    def test_reask_never_locks_a_goal_or_a_draft(self):
        state = _pending_clarification_state(text="oui", event="CONFIRM")
        result = run(cognitive_guard(state, None))
        assert "current_goal" not in result
        assert "sales_publish_draft" not in result
        assert "transaction_payload" not in result


# =====================================================================
# Observabilité (mandat §26).
# =====================================================================


class TestObservability:
    def test_resolved_decision_carries_selected_goal_and_source(self):
        state = _pending_clarification_state(text="vendre")
        result = run(cognitive_guard(state, None))
        decision = result["cognitive_decision"]
        assert decision["selected_goal"] == "SALES_PUBLISH_PRODUCT"
        assert decision["resolution_source"] == "lexical"
        assert decision["reason"] == "intent_clarification_resolved"
