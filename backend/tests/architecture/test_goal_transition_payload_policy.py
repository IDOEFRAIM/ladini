"""Politique de transition mémoire au changement de goal — audit Bloc 2,
Blocker A (2026-09-09).

Cartographie vérifiée par ce fichier (matrice PREVIOUS/CURRENT/TRANSITION
TYPE/PAYLOAD/STABLE_ENTITIES/DRAFT/MENU STATE — voir le rapport final de
l'audit pour la version complète) :

- CONTINUE (goal A → goal A, réponse de slot) : `goal_planner` (RÈGLE 1bis)
  ne purge rien ; `memory_update` fusionne la nouvelle entité sans toucher
  aux slots déjà établis.
- REFINEMENT (BUYER_REQUEST → une spécialisation, ex: BUYER_ADD_TO_CART) :
  propriété du CATALOGUE de goals (`core/goals.py::is_goal_refinement`,
  relocalisé depuis `nodes/memory.py` par cet audit — source unique,
  `goal_planner` n'a aucune logique équivalente). `memory_update` seul en a
  besoin (déclenché sur `payload["intent"]` ≠ `current_goal`, jamais par
  `goal_planner` qui ne voit jamais cette transition — elle est produite
  PAR un flow buyer hors périmètre, APRÈS que goal_planner a tourné).
- SWITCH (goal A → goal B sans rapport, hors tunnel actif) : `goal_planner`
  (RÈGLE 5) purge INCONDITIONNELLEMENT `transaction_payload`/`draft_payload`/
  `stable_entities`/`pending_interaction`/contextes de sélection AVANT que
  `memory_update` ne s'exécute — `merge_dict` + sentinelle `__reset__`.
- INTERRUPTION (goal A actif interrompu par une intention concurrente déjà
  approuvée par `cognitive_guard`, event=INTERRUPTION) : `goal_planner`
  (RÈGLE 4) applique la MÊME purge totale que SWITCH — la différence
  SWITCH/INTERRUPTION est dans QUI a décidé (RULE 5 vs RULE 4), pas dans ce
  qui est purgé.
- PRODUCT CORRECTION (même goal, produit changé en cours de tunnel) :
  propriété de `memory_update::_apply_slot` — cascade canonique sur
  quantity/unit/price/... + `stable_entities`, jamais un reset TOTAL (le
  goal reste actif, ce n'est pas un changement de goal)."""
from __future__ import annotations

from typing import Any, Dict

import agriconnect.graphs.agents.market_coach.interpreter.routing  # noqa: F401
from agriconnect.graphs.agents.market_coach.interpreter.goal_planner import goal_planner
from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from tests.conftest import StubRuntime, make_state, run


async def _merge_run(node, state: Dict[str, Any], rt) -> Dict[str, Any]:
    patch = await node(state, rt)
    merged = dict(state)
    merged.update(patch)
    return merged


def _run_planner_then_memory(**overrides) -> Dict[str, Any]:
    async def _go():
        rt = StubRuntime()
        state = make_state(**overrides)
        state = await _merge_run(goal_planner, state, rt)
        state = await _merge_run(memory_update, state, rt)
        return state

    return run(_go())


class TestContinueTransitionLosesNothing:
    def test_same_goal_slot_answer_preserves_already_collected_fields(self):
        final = _run_planner_then_memory(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="ANSWER",
            detected_intent="SALES_PUBLISH_PRODUCT",
            expected_input="PRICE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
            transaction_payload={
                "intent": "SALES_PUBLISH_PRODUCT",
                "product": "maïs",
                "quantity": 50,
                "unit": "KG",
            },
            extracted_entities={"price": 200},
        )
        payload = final.get("transaction_payload") or {}
        assert payload.get("product") == "maïs"
        assert payload.get("quantity") == 50
        assert payload.get("price") == 200
        assert final.get("current_goal") == "SALES_PUBLISH_PRODUCT"


class TestRefinementTransitionPreservesCompatibleSlots:
    def test_buyer_request_to_add_to_cart_is_recognized_as_refinement(self):
        """Propriété du catalogue vérifiée directement (source unique)."""
        from agriconnect.graphs.agents.market_coach.core.goals import (
            is_goal_refinement,
        )

        assert is_goal_refinement("BUYER_REQUEST", "BUYER_ADD_TO_CART") is True
        assert is_goal_refinement("BUYER_ADD_TO_CART", "BUYER_REQUEST") is True
        assert is_goal_refinement("SALES_PUBLISH_PRODUCT", "BUYER_ADD_TO_CART") is False

    def test_memory_update_does_not_reset_payload_on_a_refinement_bridge(self):
        """Simule la bascule produite par un flow buyer hors périmètre :
        `current_goal` porte déjà la spécialisation alors que
        `transaction_payload["intent"]` porte encore l'intention générique
        d'un tour antérieur — `memory_update` ne doit PAS purger."""
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="ANSWER",
            detected_intent="BUYER_ADD_TO_CART",
            expected_input="QUANTITY",
            working_memory={"active_goal": "BUYER_ADD_TO_CART"},
            transaction_payload={
                "intent": "BUYER_REQUEST",
                "product": "tomate",
            },
            extracted_entities={"quantity": 10, "unit": "KG"},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("product") == "tomate", (
            "le produit déjà donné pendant BUYER_REQUEST doit survivre au "
            "raffinement vers BUYER_ADD_TO_CART"
        )
        assert payload.get("quantity") == 10
        assert payload.get("intent") == "BUYER_ADD_TO_CART"


class TestSwitchTransitionClearsIncompatibleTransactionalData:
    def test_true_goal_switch_purges_the_previous_goals_slots(self):
        final = _run_planner_then_memory(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_ADD_TO_CART",
            interpreter_confidence=0.9,
            normalized_text="je voudrais acheter des tomates",
            expected_input="NONE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
            transaction_payload={
                "intent": "SALES_PUBLISH_PRODUCT",
                "product": "maïs",
                "quantity": 50,
                "unit": "KG",
            },
            extracted_entities={},
        )
        payload = final.get("transaction_payload") or {}
        assert final.get("current_goal") == "BUYER_ADD_TO_CART"
        assert payload.get("product") != "maïs"
        assert payload.get("quantity") != 50


class TestInterruptionTransitionClearsStaleSlotsExactlyLikeSwitch:
    def test_approved_interruption_leaves_no_dangerous_stale_slot(self):
        final = _run_planner_then_memory(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="INTERRUPTION",
            detected_intent="BUYER_ADD_TO_CART",
            interpreter_confidence=0.9,
            normalized_text="je voudrais acheter des tomates",
            expected_input="PRICE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
            transaction_payload={
                "intent": "SALES_PUBLISH_PRODUCT",
                "product": "maïs",
                "quantity": 50,
                "unit": "KG",
            },
            extracted_entities={},
        )
        payload = final.get("transaction_payload") or {}
        assert final.get("current_goal") == "BUYER_ADD_TO_CART"
        assert payload.get("product") != "maïs"
        assert payload.get("quantity") != 50
        assert final.get("suspended_goal") == "SALES_PUBLISH_PRODUCT"


class TestProductCorrectionCascadeIsNeverAFullReset:
    def test_product_change_mid_tunnel_cascades_but_keeps_the_same_goal(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="UPDATE",
            detected_intent="SALES_PUBLISH_PRODUCT",
            expected_input="PRICE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
            transaction_payload={
                "intent": "SALES_PUBLISH_PRODUCT",
                "product": "tomate",
                "quantity": 300,
                "unit": "KG",
                "price": 200,
            },
            extracted_entities={"product": "maïs"},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("product") == "maïs"
        assert payload.get("quantity") is None, "la quantité doit être purgée en cascade"
        assert payload.get("price") is None, "le prix doit être purgé en cascade"
        # Le GOAL, lui, n'a pas changé — ce n'est pas un SWITCH.
        assert result.get("current_goal") == "SALES_PUBLISH_PRODUCT"


class TestStableEntitiesNeverOverrideAnExplicitCurrentValue:
    """Contrat officiel (mandat §36) : `stable_entities` = données sémantiques
    réutilisables inter-tours, mais une valeur EXPLICITE du tour courant
    (`transaction_payload`) gagne toujours. Déjà garanti par construction
    dans `_apply_slot`/la boucle d'héritage (`if slot_has_value(payload.get(key)):
    continue` avant d'hériter) — ce test verrouille la propriété."""

    def test_explicit_current_product_beats_a_stable_entity(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="ANSWER",
            expected_input="PRICE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
            stable_entities={"product": "tomate", "unit": "KG", "zone": "Bobo-Dioulasso"},
            transaction_payload={"product": "maïs"},
            extracted_entities={},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("product") == "maïs", (
            "un product EXPLICITE du tour courant ne doit jamais être "
            "remplacé par la stable entity"
        )

    def test_missing_fields_are_still_inherited_from_stable_entities(self):
        """Le pendant positif du test ci-dessus : un champ ABSENT du payload
        courant doit continuer à être hérité normalement."""
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            interpreted_event="ANSWER",
            expected_input="PRICE",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
            stable_entities={"product": "tomate", "unit": "KG", "zone": "Bobo-Dioulasso"},
            transaction_payload={"product": "tomate"},
            extracted_entities={},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("zone") == "Bobo-Dioulasso"


class TestDraftPayloadNeverResurrectsAnUnrelatedProduct:
    """Contrat officiel (mandat §37) : `draft_payload` = brouillon spécialisé
    persistant d'UN workflow — il ne doit jamais ressusciter des données
    d'une opération sur un AUTRE produit. Protection déjà existante dans
    `memory_update` (garde `incoming_product`/`draft_product`) — ce test la
    verrouille explicitement avec le libellé exact du mandat."""

    def test_stale_draft_for_product_a_does_not_resurrect_onto_product_b(self):
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            interpreted_event="NEW_TASK",
            expected_input="NONE",
            working_memory={"active_goal": None},
            draft_payload={"product": "tomate", "quantity": 50, "unit": "KG"},
            transaction_payload={},
            extracted_entities={"product": "maïs"},
        )
        result = run(memory_update(state, StubRuntime()))
        payload = result.get("transaction_payload") or {}
        assert payload.get("quantity") != 50, (
            "la quantité du brouillon d'un AUTRE produit (tomate) ne doit "
            "jamais ressusciter sur le nouveau produit (maïs)"
        )
