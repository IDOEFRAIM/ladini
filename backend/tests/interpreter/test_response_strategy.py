"""`interpreter/strategy.py::response_strategy` — routeur AG-UI pur (déterministe,
zéro appel MCP). Décide QUELLE UI afficher (menu, formulaire, confirmation,
erreur, succès) à partir de l'état déjà résolu par le reste du pipeline.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.interpreter.strategy import response_strategy
from tests.conftest import make_state, run


def rs(**overrides):
    return run(response_strategy(make_state(**overrides), None))


# =====================================================================
# PRIORITÉ ABSOLUE — enrichissement forcé / sécurité
# =====================================================================

class TestForcedClarificationAndSecurity:
    def test_slot_enrichment_forced_clarification_wins_over_everything(self):
        result = rs(
            slot_enrichment_force_clarification=True,
            clarification_reasons=["ambiguous_unit"],
            status="COMPLETED",
        )
        assert result["response_strategy"] == "CLARIFICATION"
        assert result["status"] == "WAITING_INPUT"
        assert result["clarification_reasons"] == ["ambiguous_unit"]

    def test_scam_detected_blocks_immediately(self):
        result = rs(security_status="SCAM_DETECTED")
        assert result["response_strategy"] == "ERROR"
        assert result["status"] == "BLOCKED"


class TestOnboarding:
    def test_existing_onboarding_strategy_is_preserved(self):
        result = rs(response_strategy="ONBOARDING", status="WAITING_INPUT")
        assert result["response_strategy"] == "ONBOARDING"

    def test_is_onboarding_flag_forces_waiting_input(self):
        result = rs(is_onboarding=True, status="COMPLETED")
        assert result["response_strategy"] == "ONBOARDING"
        assert result["status"] == "WAITING_INPUT"


# =====================================================================
# INTERRUPTION
# =====================================================================

class TestInterruption:
    def test_upstream_concrete_ui_is_preserved_on_interruption(self):
        result = rs(interruption_detected=True, response_strategy="SELECTION_MENU")
        assert result["response_strategy"] == "SELECTION_MENU"

    def test_confirmation_strategy_sets_waiting_confirmation_status(self):
        result = rs(interruption_detected=True, response_strategy="CONFIRMATION")
        assert result["status"] == "WAITING_CONFIRMATION"

    def test_ask_missing_field_strategy_sets_waiting_input_status(self):
        result = rs(interruption_detected=True, response_strategy="ASK_MISSING_FIELD")
        assert result["status"] == "WAITING_INPUT"

    def test_interruption_with_pending_selection_shows_the_menu(self):
        result = rs(interruption_detected=True, expected_input="SELECTION", pending_menu={"x": 1})
        assert result["response_strategy"] == "SELECTION_MENU"

    def test_interruption_with_pending_confirmation(self):
        result = rs(interruption_detected=True, expected_input="CONFIRMATION")
        assert result["response_strategy"] == "CONFIRMATION"
        assert result["status"] == "WAITING_CONFIRMATION"

    def test_bare_interruption_falls_back_to_handler(self):
        result = rs(interruption_detected=True, expected_input="NONE")
        assert result["response_strategy"] == "INTERRUPTION_HANDLER"

    def test_interpreted_event_interruption_also_triggers_the_branch(self):
        result = rs(interpreted_event="INTERRUPTION", expected_input="NONE")
        assert result["response_strategy"] == "INTERRUPTION_HANDLER"

    def test_existing_interruption_handler_strategy_also_triggers_the_branch(self):
        result = rs(response_strategy="INTERRUPTION_HANDLER", expected_input="NONE")
        assert result["response_strategy"] == "INTERRUPTION_HANDLER"


class TestReject:
    def test_reject_always_clarifies_regardless_of_missing_fields(self):
        result = rs(interpreted_event="REJECT", missing_fields=["price"])
        assert result["response_strategy"] == "CLARIFICATION"
        assert result["status"] == "WAITING_INPUT"


# =====================================================================
# COGNITIVE GUARD
# =====================================================================

class TestCognitiveGuard:
    def test_recover_active_tunnel_shows_pending_menu(self):
        result = rs(
            cognitive_decision={"action": "recover_active_tunnel"},
            expected_input="SELECTION",
            expected_candidates=["a", "b"],
        )
        assert result["response_strategy"] == "SELECTION_MENU"

    def test_recover_active_tunnel_shows_confirmation(self):
        result = rs(cognitive_decision={"action": "recover_active_tunnel"}, expected_input="CONFIRMATION")
        assert result["response_strategy"] == "CONFIRMATION"

    def test_recover_active_tunnel_defaults_to_recovery(self):
        result = rs(cognitive_decision={"action": "recover_active_tunnel"}, expected_input="NONE")
        assert result["response_strategy"] == "RECOVERY"

    def test_abandon_tunnel_max_retries_clarifies(self):
        result = rs(cognitive_decision={"action": "abandon_tunnel_max_retries"})
        assert result["response_strategy"] == "CLARIFICATION"


# =====================================================================
# UNKNOWN / OUT_OF_SCOPE
# =====================================================================

class TestUnknownOutOfScope:
    @pytest.mark.parametrize("event", ["UNKNOWN", "OUT_OF_SCOPE"])
    def test_selection_menu_is_shown_when_candidates_pending(self, event):
        result = rs(interpreted_event=event, expected_input="SELECTION", expected_candidates=["a"])
        assert result["response_strategy"] == "SELECTION_MENU"

    def test_slot_filling_input_asks_for_the_missing_field(self):
        result = rs(interpreted_event="UNKNOWN", expected_input="QUANTITY")
        assert result["response_strategy"] == "ASK_MISSING_FIELD"

    def test_confirmation_input_re_shows_confirmation(self):
        result = rs(interpreted_event="UNKNOWN", expected_input="CONFIRMATION")
        assert result["response_strategy"] == "CONFIRMATION"

    def test_bare_unknown_always_clarifies_even_with_a_stale_completed_status(self):
        """Ne doit JAMAIS retomber sur SUCCESS via un `status`/`execution_result`
        périmé — UNKNOWN hors tunnel doit toujours clarifier."""
        result = rs(interpreted_event="UNKNOWN", status="COMPLETED", execution_result={"ok": True})
        assert result["response_strategy"] == "CLARIFICATION"


# =====================================================================
# SLOT-FILLING (missing_fields)
# =====================================================================

class TestMissingFields:
    def test_missing_fields_forces_ask_missing_field(self):
        result = rs(interpreted_event="NEW_TASK", missing_fields=["price"])
        assert result["response_strategy"] == "ASK_MISSING_FIELD"
        assert result["status"] == "WAITING_INPUT"

    def test_last_missing_field_alone_also_forces_it(self):
        result = rs(interpreted_event="NEW_TASK", last_missing_field="quantity")
        assert result["response_strategy"] == "ASK_MISSING_FIELD"


# =====================================================================
# UPSTREAM STRATEGY PRESERVATION
# =====================================================================

class TestUpstreamStrategyPreservation:
    @pytest.mark.parametrize("strategy", ["SELECTION_MENU", "ASK_MISSING_FIELD", "ERROR", "CONFIRMATION", "SUCCESS"])
    def test_concrete_upstream_strategies_are_preserved(self, strategy):
        result = rs(interpreted_event="NEW_TASK", response_strategy=strategy)
        assert result["response_strategy"] == strategy

    def test_ag_ui_component_is_forwarded_when_present(self):
        result = rs(interpreted_event="NEW_TASK", response_strategy="SUCCESS", ag_ui_component={"type": "card"})
        assert result["ag_ui_component"] == {"type": "card"}

    def test_recovery_strategy_is_preserved(self):
        result = rs(interpreted_event="NEW_TASK", response_strategy="RECOVERY")
        assert result["response_strategy"] == "RECOVERY"
        assert result["status"] == "WAITING_INPUT"


# =====================================================================
# STATUT DE BASE (status)
# =====================================================================

class TestBaseStatus:
    @pytest.mark.parametrize("status", ["BLOCKED", "ERROR"])
    def test_blocked_or_error_status_yields_error_strategy(self, status):
        result = rs(interpreted_event="NEW_TASK", status=status)
        assert result["response_strategy"] == "ERROR"

    def test_completed_status_yields_success(self):
        result = rs(interpreted_event="NEW_TASK", status="COMPLETED")
        assert result["response_strategy"] == "SUCCESS"

    def test_no_goal_with_execution_result_yields_success(self):
        result = rs(interpreted_event="NEW_TASK", current_goal=None, execution_result={"ok": True})
        assert result["response_strategy"] == "SUCCESS"

    def test_waiting_confirmation_status_yields_confirmation(self):
        result = rs(interpreted_event="NEW_TASK", status="WAITING_CONFIRMATION")
        assert result["response_strategy"] == "CONFIRMATION"
        assert result["status"] == "WAITING_CONFIRMATION"

    def test_confirmation_expected_input_yields_confirmation_even_without_status(self):
        result = rs(interpreted_event="NEW_TASK", expected_input="CONFIRMATION")
        assert result["response_strategy"] == "CONFIRMATION"

    def test_waiting_input_status_with_selection_shows_menu(self):
        result = rs(interpreted_event="NEW_TASK", status="WAITING_INPUT", expected_input="SELECTION", expected_candidates=["a"])
        assert result["response_strategy"] == "SELECTION_MENU"

    def test_waiting_input_status_with_missing_field_asks_for_it(self):
        result = rs(interpreted_event="NEW_TASK", status="WAITING_INPUT", expected_input="QUANTITY")
        assert result["response_strategy"] == "ASK_MISSING_FIELD"

    def test_default_fallback_is_clarification(self):
        result = rs(interpreted_event="NEW_TASK")
        assert result["response_strategy"] == "CLARIFICATION"

    def test_unknown_event_with_no_context_also_clarifies(self):
        """Le repli par défaut de `make_state()` (`interpreted_event="UNKNOWN"`)
        doit lui-même clarifier hors tout contexte de tunnel — verrouillé
        séparément de la classe `TestUnknownOutOfScope` ci-dessus."""
        result = rs()
        assert result["response_strategy"] == "CLARIFICATION"
