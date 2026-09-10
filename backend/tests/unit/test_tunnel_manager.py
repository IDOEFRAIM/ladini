"""`core/tunnel_manager.py::TunnelManager.evaluate` — contrat direct (audit
Bloc 2, 2026-09-09).

Avant cet audit, ce module n'avait AUCUN test dédié — seule sa consommation
indirecte via `goal_planner` (tests/interpreter/test_goal_planner_state_machine.py)
l'exerçait partiellement. Ce fichier verrouille son contrat en isolation :
`cognitive_guard` (Bloc 1, gelé) est l'UNIQUE autorité de la décision
« confiance suffisante pour interrompre ? » (seuil 0.85, nodes/cognitive.py).
`TunnelManager` ne doit plus jamais re-décider cette même question à un seuil
différent pour un événement `NEW_TASK` — seule une intention de breakout
critique (déterministe, indépendante de la confiance) ou un événement
`INTERRUPTION` déjà approuvé en amont peuvent casser un tunnel actif."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.tunnel_manager import (
    TunnelManager,
    tunnel_manager,
)


class TestNoActiveTunnel:
    def test_no_current_goal_never_locks_anything(self):
        td = tunnel_manager.evaluate(
            current_goal=None,
            expected_input="PRODUCT",
            incoming_event="NEW_TASK",
            incoming_intent="BUYER_REQUEST",
            confidence=0.99,
        )
        assert td.stay_in_tunnel is False
        assert td.allow_interrupt is False
        assert td.reason == "no_active_tunnel"

    def test_no_expected_input_never_locks_anything(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="NONE",
            incoming_event="NEW_TASK",
            incoming_intent="BUYER_REQUEST",
            confidence=0.99,
        )
        assert td.reason == "no_active_tunnel"


class TestSlotEventsAreAlwaysAbsorbed:
    def test_confirm_selection_answer_update_reject_all_stay(self):
        for event in ("CONFIRM", "SELECTION", "ANSWER", "UPDATE", "REJECT"):
            td = tunnel_manager.evaluate(
                current_goal="SALES_PUBLISH_PRODUCT",
                expected_input="PRODUCT",
                incoming_event=event,
                incoming_intent="UNKNOWN",
                confidence=0.0,
            )
            assert td.stay_in_tunnel is True
            assert td.allow_interrupt is False
            assert td.reason == "slot_event"


class TestAlwaysUnbreakableInputs:
    def test_otp_code_never_interrupted_even_at_full_confidence(self):
        td = tunnel_manager.evaluate(
            current_goal="PRODUCER_CONFIRM_DELIVERY_OTP",
            expected_input="OTP_CODE",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_REQUEST",
            confidence=1.0,
        )
        assert td.stay_in_tunnel is True
        assert td.allow_interrupt is False
        assert td.reason == "always_unbreakable"

    def test_otp_code_beats_even_a_breakout_navigation_intent(self):
        """L'OTP (code de paiement/livraison) est PLUS prioritaire que le
        breakout critique — vérifié par l'ordre des gardes dans `evaluate`."""
        td = tunnel_manager.evaluate(
            current_goal="PRODUCER_CONFIRM_DELIVERY_OTP",
            expected_input="OTP_CODE",
            incoming_event="NEW_TASK",
            incoming_intent="BUYER_VIEW_CART",
            confidence=1.0,
        )
        assert td.reason == "always_unbreakable"
        assert td.allow_interrupt is False


class TestTunnelManagerNoLongerDecidesBreakouts:
    """(2026-09-09, Bloc 2 passe finale — Invariant A) : contrat CHANGE
    deliberement. Ce noeud decidait auparavant lui-meme qu'une intention de
    navigation critique cassait le tunnel (reason="critical_intent"), en
    aval et sans consulter `cognitive_guard` — deux autorites sur la meme
    question. Il ne decide plus : il ne fait qu'INTERDIRE (OTP) ou laisser
    passer une transition deja approuvee (`policy_approved`).

    Le comportement UTILISATEUR est inchange : le breakout fonctionne
    toujours, decide un cran plus haut — preuve de bout en bout dans
    `tests/architecture/test_interruption_ownership.py`."""

    def test_new_task_with_a_breakout_intent_no_longer_switches_here(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            incoming_event="NEW_TASK",
            incoming_intent="BUYER_VIEW_CART",
            confidence=0.0,
        )
        assert td.stay_in_tunnel is True
        assert td.allow_interrupt is False

    def test_breakout_intent_on_a_hard_slot_stays_locked_too(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            incoming_event="NEW_TASK",
            incoming_intent="BUYER_VIEW_CART",
            confidence=0.0,
        )
        assert td.stay_in_tunnel is True
        assert td.allow_interrupt is False


class TestPolicyApprovedInterruption:
    """`policy_approved=True` = « `cognitive_guard` a tranche ». Ce noeud
    n'oppose plus alors qu'un seul veto : l'invariant structurel OTP."""

    def test_policy_approved_crosses_a_soft_slot_at_zero_confidence(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_VIEW_CART",
            confidence=0.0,
            policy_approved=True,
        )
        assert td.allow_interrupt is True
        assert td.reason == "policy_approved_interruption"

    def test_policy_approved_crosses_a_hard_confirmation_slot(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_VIEW_CART",
            confidence=0.0,
            policy_approved=True,
        )
        assert td.allow_interrupt is True
        assert td.reason == "policy_approved_interruption"

    def test_policy_approved_never_crosses_the_otp_invariant(self):
        """Invariant STRUCTUREL non negociable (mandat §6)."""
        td = tunnel_manager.evaluate(
            current_goal="PRODUCER_CONFIRM_DELIVERY_OTP",
            expected_input="OTP_CODE",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_VIEW_CART",
            confidence=1.0,
            policy_approved=True,
        )
        assert td.stay_in_tunnel is True
        assert td.allow_interrupt is False
        assert td.reason == "always_unbreakable"

    def test_an_unapproved_interruption_still_faces_the_historic_threshold(self):
        """Une INTERRUPTION non produite par `cognitive_guard` (ex:
        `input_interpreter` sur derive pendant SELECTION/CONFIRMATION,
        Bloc 1 gele) garde EXACTEMENT le comportement d'avant."""
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_REQUEST",
            confidence=0.10,
        )
        assert td.allow_interrupt is False
        assert td.reason == "interruption_low_confidence"


class TestNewTaskNeverIndependentlyInterrupts:
    """(2026-09-09, audit Bloc 2 — bug trouvé et corrigé) : avant ce fix,
    `NEW_TASK` sur un slot souple OU dur pouvait interrompre dès confiance
    >= 0.60 — un second seuil, plus bas que celui de `cognitive_guard`
    (0.85), qui pouvait renverser un refus déjà décidé en amont."""

    def test_new_task_on_soft_slot_stays_locked_even_at_high_confidence(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            incoming_event="NEW_TASK",
            incoming_intent="BUYER_REQUEST",
            confidence=0.99,
        )
        assert td.stay_in_tunnel is True
        assert td.allow_interrupt is False
        assert td.reason == "default_lock"

    def test_new_task_on_hard_slot_stays_locked_even_at_high_confidence(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            incoming_event="NEW_TASK",
            incoming_intent="BUYER_REQUEST",
            confidence=0.99,
        )
        assert td.stay_in_tunnel is True
        assert td.allow_interrupt is False
        assert td.reason == "hard_slot"


class TestInterruptionEventIsHonoured:
    """`INTERRUPTION` n'atteint ce module qu'après approbation de
    `cognitive_guard` (seuil 0.85) — le seuil ici (0.60) est une
    confirmation redondante et inoffensive, jamais une seconde décision
    réelle dans le pipeline actuel (un seul appelant : `goal_planner`)."""

    def test_interruption_on_soft_slot_is_allowed_above_threshold(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_REQUEST",
            confidence=0.85,
        )
        assert td.stay_in_tunnel is False
        assert td.allow_interrupt is True
        assert td.reason == "interruption_threshold_met"

    def test_interruption_on_hard_slot_is_allowed_above_threshold(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="CONFIRMATION",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_REQUEST",
            confidence=0.85,
        )
        assert td.stay_in_tunnel is False
        assert td.allow_interrupt is True
        assert td.reason == "hard_slot_high_confidence"

    def test_interruption_below_threshold_is_blocked(self):
        td = tunnel_manager.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_REQUEST",
            confidence=0.10,
        )
        assert td.stay_in_tunnel is True
        assert td.allow_interrupt is False
        assert td.reason == "interruption_low_confidence"


class TestCustomThresholdIsHonoured:
    """L'instance module-level n'est pas la seule forme possible — un appelant
    de test peut fournir un seuil personnalisé."""

    def test_custom_threshold_changes_the_interruption_boundary(self):
        lenient = TunnelManager(interruption_threshold=0.20)
        td = lenient.evaluate(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRODUCT",
            incoming_event="INTERRUPTION",
            incoming_intent="BUYER_REQUEST",
            confidence=0.25,
        )
        assert td.allow_interrupt is True
