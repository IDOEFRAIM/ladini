"""BLOC 2 — passe finale (2026-09-09), Invariant A : « QUI décide que le
goal actif est interrompu ? »

Réponse unique et testée ici : `nodes/cognitive.py::cognitive_guard`.

Avant cette passe il y avait DEUX autorités :
  - `cognitive_guard` (seuil de confiance 0.85 sur une intention concurrente) ;
  - `core/tunnel_manager.py` (branche `critical_intent`, indépendante de la
    confiance et JAMAIS consultée par la première) — une intention de
    navigation à confiance 0.2 cassait donc un tunnel que `cognitive_guard`
    venait explicitement de refuser d'interrompre.

Après : `cognitive_guard` décide (confiance OU breakout de navigation),
`goal_planner` applique, `TunnelManager` ne garde que le droit d'INTERDIRE
un invariant structurel (OTP). Ces tests exercent la CHAÎNE RÉELLE, pas
chaque nœud isolément.
"""
from __future__ import annotations

from typing import Any, Dict

# Import obligatoire : peuple INTENT_TO_GOAL_MAP consommé par goal_planner.
import agriconnect.graphs.agents.market_coach.interpreter.routing  # noqa: F401
from agriconnect.graphs.agents.market_coach.core.conversation_decision import (
    ConversationAction,
)
from agriconnect.graphs.agents.market_coach.core.goals import (
    NAVIGATION_BREAKOUT_GOALS,
)
from agriconnect.graphs.agents.market_coach.core.tunnel_manager import tunnel_manager
from agriconnect.graphs.agents.market_coach.interpreter.goal_planner import goal_planner
from agriconnect.graphs.agents.market_coach.nodes.cognitive import cognitive_guard
from tests.conftest import make_state, run


def _chain(**overrides) -> Dict[str, Any]:
    """cognitive_guard -> goal_planner, sur le MÊME état — la séquence
    réelle du graphe compilé pour un tour en tunnel."""
    # `goal_planner` re-verrouille le goal sur tout texte <= 4 caracteres
    # (garde "bruit court", anterieur a ce chantier et hors perimetre) : les
    # fixtures fournissent donc un enonce realiste, comme en production.
    overrides.setdefault("normalized_text", "je veux voir autre chose maintenant")
    state = make_state(**overrides)
    guard_patch = run(cognitive_guard(state, None))
    merged = {**state, **guard_patch}
    planner_patch = run(goal_planner(merged, None))
    return {"guard": guard_patch, "planner": planner_patch, "state": merged}


class TestOwnerIsCognitiveGuard:
    def test_high_confidence_competing_intent_interrupts_through_the_full_chain(self):
        r = _chain(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.96,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["guard"]["interpreted_event"] == "INTERRUPTION"
        decision = r["guard"]["cognitive_decision"]
        assert decision["action"] == ConversationAction.INTERRUPT_ACTIVE_GOAL
        assert decision["reason"] == "competing_intent_high_confidence"
        assert r["planner"]["current_goal"] == "BUYER_REQUEST"
        assert r["planner"]["suspended_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_low_confidence_competitor_never_interrupts(self):
        r = _chain(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.30,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["guard"].get("interpreted_event") != "INTERRUPTION"
        assert (
            r["guard"]["cognitive_decision"]["action"]
            != ConversationAction.INTERRUPT_ACTIVE_GOAL
        )
        assert r["planner"]["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_critical_navigation_breakout_interrupts_at_zero_confidence(self):
        """Le breakout reste indépendant de la confiance — mais c'est
        désormais `cognitive_guard` qui le décide, pas `TunnelManager`."""
        assert "BUYER_VIEW_CART" in NAVIGATION_BREAKOUT_GOALS
        r = _chain(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_VIEW_CART",
            interpreter_confidence=0.0,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        decision = r["guard"]["cognitive_decision"]
        assert decision["action"] == ConversationAction.INTERRUPT_ACTIVE_GOAL
        assert decision["reason"] == "critical_navigation_breakout"
        assert r["planner"]["current_goal"] == "BUYER_VIEW_CART"
        assert r["planner"]["suspended_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_breakout_also_crosses_a_hard_confirmation_slot(self):
        r = _chain(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_VIEW_CART",
            interpreter_confidence=0.0,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert r["planner"]["current_goal"] == "BUYER_VIEW_CART"


class TestTunnelManagerOnlyForbidsNeverChooses:
    def test_tunnel_manager_alone_would_not_have_broken_the_tunnel(self):
        """Preuve du décideur UNIQUE : présenté le MÊME tour sans décision
        amont, `TunnelManager` ne casse rien. La bascule du test précédent
        vient donc entièrement de `cognitive_guard`."""
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            incoming_event="NEW_TASK",
            incoming_intent="BUYER_VIEW_CART",
            confidence=0.0,
        )
        assert td.allow_interrupt is False

    def test_otp_invariant_survives_a_critical_breakout_through_the_full_chain(self):
        """Mandat §6/§8 : un invariant structurel (code OTP de
        paiement/livraison) n'est PAS une policy conversationnelle — il
        gagne même contre une interruption approuvée par `cognitive_guard`."""
        r = _chain(
            interpreted_event="NEW_TASK",
            detected_intent="BUYER_VIEW_CART",
            interpreter_confidence=1.0,
            current_goal="PRODUCER_CONFIRM_DELIVERY_OTP",
            expected_input="OTP_CODE",
            working_memory={"active_goal": "PRODUCER_CONFIRM_DELIVERY_OTP"},
        )
        # La policy a bien approuvé (elle ignore les invariants structurels)…
        assert (
            r["guard"]["cognitive_decision"]["action"]
            == ConversationAction.INTERRUPT_ACTIVE_GOAL
        )
        # … mais le tunnel OTP survit : TunnelManager oppose son veto.
        assert r["planner"]["current_goal"] == "PRODUCER_CONFIRM_DELIVERY_OTP"
        assert "suspended_goal" not in r["planner"]


class TestUpstreamInterruptionsAreRatifiedByTheSameOwner:
    """`input_interpreter` (Bloc 1, GELÉ) émet lui aussi des INTERRUPTION
    (dérive pendant SELECTION/CONFIRMATION, seuil 0.60). Elles ne passaient
    par AUCUNE décision de `cognitive_guard` : `TunnelManager` tranchait
    seul, avec son propre seuil. Elles sont désormais RATIFIÉES — même
    critère, donc comportement inchangé, mais UN seul approbateur."""

    def test_an_upstream_interruption_above_threshold_is_ratified(self):
        r = _chain(
            interpreted_event="INTERRUPTION",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.70,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="SELECTION",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        decision = r["guard"]["cognitive_decision"]
        assert decision["action"] == ConversationAction.INTERRUPT_ACTIVE_GOAL
        assert decision["reason"] == "ratified_upstream_interruption"
        assert r["planner"]["current_goal"] == "BUYER_REQUEST"

    def test_an_upstream_interruption_below_threshold_is_not_ratified_and_dies(self):
        """Verdicts concordants par construction (même seuil) : non ratifiée
        ici, refusée en aval — le tunnel survit."""
        r = _chain(
            interpreted_event="INTERRUPTION",
            detected_intent="BUYER_REQUEST",
            interpreter_confidence=0.20,
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="SELECTION",
            working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
        )
        assert (
            r["guard"]["cognitive_decision"]["action"]
            != ConversationAction.INTERRUPT_ACTIVE_GOAL
        )
        assert r["planner"]["current_goal"] == "SALES_PUBLISH_PRODUCT"

    def test_every_applied_interruption_carries_the_owner_approval(self):
        """Propriété d'ownership testée directement : sur l'ensemble des
        scénarios d'interruption RÉELLEMENT appliqués, `cognitive_decision.
        action` vaut TOUJOURS INTERRUPT_ACTIVE_GOAL."""
        scenarios = [
            dict(interpreted_event="NEW_TASK", detected_intent="BUYER_REQUEST",
                 interpreter_confidence=0.96),
            dict(interpreted_event="NEW_TASK", detected_intent="BUYER_VIEW_CART",
                 interpreter_confidence=0.0),
            dict(interpreted_event="INTERRUPTION", detected_intent="BUYER_REQUEST",
                 interpreter_confidence=0.70),
        ]
        for extra in scenarios:
            r = _chain(
                current_goal="SALES_PUBLISH_PRODUCT",
                expected_input="PRODUCT",
                working_memory={"active_goal": "SALES_PUBLISH_PRODUCT"},
                **extra,
            )
            switched = r["planner"].get("current_goal") != "SALES_PUBLISH_PRODUCT"
            approved = (
                r["guard"]["cognitive_decision"]["action"]
                == ConversationAction.INTERRUPT_ACTIVE_GOAL
            )
            assert switched is True, f"scénario non appliqué : {extra}"
            assert approved is True, f"interruption appliquée sans approbation : {extra}"
