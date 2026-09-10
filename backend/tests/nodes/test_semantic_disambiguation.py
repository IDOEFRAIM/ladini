"""`nodes/semantic_disambiguation.py` — EXÉCUTEUR d'une décision DISAMBIGUATE
déjà prise par `cognitive_guard` (2026-09-08, correction topologique du bloc
conversationnel, mandat §12/§14).

Avant ce correctif, ce nœud recalculait lui-même « faut-il désambiguïser ? »
(event ∈ {NEW_TASK, UNKNOWN}, absence de `pending_interaction`, seuil de
confiance LLM) — voir `tests/nodes/test_nodes_behaviour.py::TestDisambiguation`
pour ces mêmes scénarios de DÉCISION, désormais retargetés sur
`cognitive_guard` (le nouveau propriétaire). Ce fichier-ci ne teste plus que
l'EXÉCUTION : donné un `disambiguation_candidate` déjà posé, ce nœud
construit-il correctement le menu, sans re-décider quoi que ce soit ?"""
from __future__ import annotations

from ladini.graphs.agents.market_coach.nodes.semantic_disambiguation import (
    semantic_disambiguation,
)
from tests.conftest import make_state, run

_CANDIDATE = {
    "id": "STOCK_OR_SALES_DECLARATION",
    "title": "C'est prêt à vendre maintenant, ou pas encore ?",
    "pedagogical_hint": "Choisissez l'option qui correspond.",
    "options": [
        ("SALES_PUBLISH_PRODUCT", "Prêt maintenant"),
        ("DECLARE_CROP_CYCLE", "Prêt plus tard"),
    ],
    "candidates": ["SALES_PUBLISH_PRODUCT", "DECLARE_CROP_CYCLE"],
}


class TestSemanticDisambiguationTrustsThePrecomputedCandidate:
    def test_builds_a_menu_from_the_precomputed_candidate_regardless_of_confidence(self):
        """Preuve centrale du nouveau contrat : même une confiance LLM
        ÉLEVÉE et un `detected_intent` déjà tranché (qui, AVANT ce
        correctif, faisait court-circuiter ce nœud en {} — voir l'ancien
        garde `_DISAMBIGUATION_CONFIDENCE_THRESHOLD` retiré d'ici) ne
        l'empêchent plus de construire le menu : cette décision a déjà été
        prise par `cognitive_guard` en amont (voir
        `nodes/cognitive.py::_classify_nominal_action`), ce nœud l'exécute
        sans redemander son avis."""
        state = make_state(
            interpreted_event="NEW_TASK",
            interpreter_confidence=0.97,
            detected_intent="SALES_PUBLISH_PRODUCT",
            normalized_text="j ai 200 kg de tomates a vendre",
            disambiguation_candidate=_CANDIDATE,
            cognitive_decision={"action": "DISAMBIGUATE"},
        )
        result = run(semantic_disambiguation(state, None))
        assert result["current_goal"] == "DISAMBIGUATION_PENDING"
        assert result["response_strategy"] == "SELECTION_MENU"
        assert result["available_mapping"] == {
            "1": "SALES_PUBLISH_PRODUCT",
            "2": "DECLARE_CROP_CYCLE",
        }

    def test_does_not_reimport_or_recompute_event_based_gating(self):
        """Preuve structurelle (mandat §14) : le module ne contient plus
        aucune trace des conditions qu'il vérifiait lui-même avant ce
        correctif (event, already_expecting, seuil de confiance) — la
        seule policy de désambiguïsation vit désormais dans
        `cognitive_guard`."""
        import inspect

        import ladini.graphs.agents.market_coach.nodes.semantic_disambiguation as mod

        source = inspect.getsource(mod.semantic_disambiguation)
        assert "already_expecting" not in source
        assert "interpreted_event" not in source
        assert "_DISAMBIGUATION_CONFIDENCE_THRESHOLD" not in source


class TestSemanticDisambiguationCompatibilityFallback:
    """Mandat §13 : repli autorisé UNIQUEMENT si `disambiguation_candidate`
    est absent (ancien checkpoint, ou appel direct hors du graphe compilé
    comme ici) — recalcule alors localement via
    `_detect_disambiguation_candidates`, documenté comme repli de
    compatibilité, pas une seconde policy."""

    def test_falls_back_to_the_local_lexical_computation_when_candidate_is_missing(self):
        state = make_state(
            interpreted_event="NEW_TASK",
            interpreter_confidence=0.3,
            detected_intent="UNKNOWN",
            normalized_text="j ai 200 kg de tomates",
            user_role="PRODUCER",
            cognitive_decision={"action": "DISAMBIGUATE"},
        )
        assert "disambiguation_candidate" not in state
        result = run(semantic_disambiguation(state, None))
        assert result["current_goal"] == "DISAMBIGUATION_PENDING"

    def test_no_candidate_found_anywhere_is_a_no_op(self):
        state = make_state(
            interpreted_event="NEW_TASK",
            normalized_text="bonjour comment allez-vous",
            cognitive_decision={"action": "DISAMBIGUATE"},
        )
        result = run(semantic_disambiguation(state, None))
        assert result == {}


class TestSemanticDisambiguationStructuralGuards:
    def test_a_candidate_with_a_single_option_is_a_no_op(self):
        state = make_state(
            disambiguation_candidate={
                "id": "X", "options": [("A", "a")], "candidates": ["A"],
            },
            cognitive_decision={"action": "DISAMBIGUATE"},
        )
        result = run(semantic_disambiguation(state, None))
        assert result == {}

    def test_stashes_already_extracted_entities_into_transaction_payload(self):
        state = make_state(
            disambiguation_candidate=_CANDIDATE,
            cognitive_decision={"action": "DISAMBIGUATE"},
            extracted_entities={"quantity": 958, "price": 375},
            transaction_payload={},
        )
        result = run(semantic_disambiguation(state, None))
        assert result["transaction_payload"]["quantity"] == 958
        assert result["transaction_payload"]["price"] == 375
