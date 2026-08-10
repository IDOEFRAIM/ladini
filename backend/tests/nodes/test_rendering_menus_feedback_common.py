"""`nodes/rendering/menus.py` + `feedback.py` + gaps in `common.py` — rendu
AG-UI pur (aucun appel MCP), le dernier maillon avant que le texte parte au
WhatsApp de l'utilisateur.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.nodes.rendering.common import RenderContext
from tests.conftest import run


def ctx(**state_overrides):
    from tests.conftest import make_state
    state = make_state(**state_overrides)
    return RenderContext(
        state=state, mc_runtime=None,
        strategy=str(state.get("response_strategy") or ""),
        status=str(state.get("status") or ""),
        goal=state.get("current_goal"),
        salutation="",
        payload=state.get("transaction_payload") or {},
    )


# =====================================================================
# render_selection_menu
# =====================================================================

class TestRenderSelectionMenu:
    def test_reuses_a_precomputed_final_response(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.menus import render_selection_menu
        c = ctx(final_response="menu précalculé", ag_ui_component={"x": 1})
        result = run(render_selection_menu(c))
        assert result["final_response"] == "menu précalculé"
        assert result["ag_ui_component"] == {"x": 1}

    def test_no_goal_asks_generically(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.menus import render_selection_menu
        c = ctx(current_goal=None)
        result = run(render_selection_menu(c))
        assert "choisir" in result["final_response"]

    def test_preformatted_working_memory_menu_is_used_verbatim(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.menus import render_selection_menu
        c = ctx(current_goal="BUYER_LIST_AUCTIONS", working_memory={"auction_menu": "Menu préformaté"})
        result = run(render_selection_menu(c))
        assert result["final_response"] == "Menu préformaté"

    def test_candidates_are_numbered_when_no_preformatted_menu(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.menus import render_selection_menu
        c = ctx(current_goal="BUYER_LIST_AUCTIONS", expected_candidates=["Mais", "Riz"])
        result = run(render_selection_menu(c))
        assert "1. Mais" in result["final_response"]
        assert "2. Riz" in result["final_response"]
        assert "numéro" in result["final_response"]
        assert len(result["ag_ui_component"]["kwargs"]["options"]) == 2

    def test_does_not_duplicate_the_reply_hint_if_already_present(self):
        """Le préformaté peut déjà contenir « répondez… » — pas de doublon."""
        from agriconnect.graphs.agents.market_coach.nodes.rendering.menus import render_selection_menu
        c = ctx(current_goal="BUYER_LIST_AUCTIONS", working_memory={"bids_menu": "1. X\nRépondez par le numéro"})
        result = run(render_selection_menu(c))
        assert result["final_response"].lower().count("répondez") == 1


# =====================================================================
# render_error
# =====================================================================

class TestRenderError:
    def test_scam_detected_shows_a_security_alert(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_error
        c = ctx(security_status="SCAM_DETECTED")
        result = run(render_error(c))
        assert "sécurité" in result["final_response"].lower()
        assert result["ag_ui_component"]["kwargs"]["reason"] == "Sécurité renforcée"

    def test_missing_unit_warning_shows_a_specific_message(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_error
        c = ctx(security_status="WARNING")
        result = run(render_error(c))
        assert "unité" in result["final_response"].lower()

    def test_validation_error_reason_is_shown(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_error
        c = ctx(validation_errors=["stock insuffisant"])
        result = run(render_error(c))
        assert "stock insuffisant" in result["final_response"]

    def test_security_reason_is_used_when_no_validation_errors(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_error
        c = ctx(security_reason="raison spécifique")
        result = run(render_error(c))
        assert "raison spécifique" in result["final_response"]

    def test_generic_technical_error_when_no_reason_available(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_error
        c = ctx()
        result = run(render_error(c))
        assert "erreur technique" in result["final_response"].lower()

    def test_known_goal_offers_a_retry_hint(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_error
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", validation_errors=["x"])
        result = run(render_error(c))
        assert "annuler" in result["final_response"]


# =====================================================================
# render_recovery
# =====================================================================

class TestRenderRecovery:
    def test_max_retries_pauses_the_operation(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_recovery
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", retry_count=2)
        result = run(render_recovery(c))
        assert "mise en pause" in result["final_response"]

    def test_below_max_retries_asks_for_the_missing_field_with_reason(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_recovery
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", retry_count=0, last_missing_field="price")
        result = run(render_recovery(c))
        assert "prix" in result["final_response"].lower()
        assert result["retry_count"] == 1

    def test_falls_back_to_form_step_when_no_missing_field(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_recovery
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", retry_count=0, form_step="quantity")
        result = run(render_recovery(c))
        assert result["final_response"]

    def test_falls_back_to_expected_input_when_no_field_or_form_step(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_recovery
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT", retry_count=0, expected_input="QUANTITY")
        result = run(render_recovery(c))
        assert result["final_response"]

    def test_no_goal_uses_generic_operation_label(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_recovery
        c = ctx(current_goal=None, retry_count=0)
        result = run(render_recovery(c))
        assert "votre opération" in result["final_response"]


# =====================================================================
# render_interruption
# =====================================================================

class TestRenderInterruption:
    def test_preorder_init_has_a_dedicated_message(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_interruption
        c = ctx(detected_intent="BUYER_PREORDER_INIT")
        result = run(render_interruption(c))
        assert "précommande" in result["final_response"]
        assert result["ag_ui_component"] is None

    def test_no_suspended_goal_uses_generic_head(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_interruption
        c = ctx(current_goal=None, suspended_goal=None)
        result = run(render_interruption(c))
        assert "nouvelle demande" in result["final_response"]

    def test_current_goal_label_replaces_generic_head(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_interruption
        c = ctx(current_goal="SALES_PUBLISH_PRODUCT")
        result = run(render_interruption(c))
        assert "Je passe à" in result["final_response"]

    def test_suspended_goal_with_selection_hint(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_interruption
        c = ctx(suspended_goal="SALES_PUBLISH_PRODUCT", expected_input="SELECTION")
        result = run(render_interruption(c))
        assert "numéro du menu" in result["final_response"]

    def test_suspended_goal_with_confirmation_hint(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_interruption
        c = ctx(suspended_goal="SALES_PUBLISH_PRODUCT", expected_input="CONFIRMATION")
        result = run(render_interruption(c))
        assert "oui / non" in result["final_response"]

    def test_suspended_goal_without_expected_input_offers_resume(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_interruption
        c = ctx(suspended_goal="SALES_PUBLISH_PRODUCT", expected_input="NONE")
        result = run(render_interruption(c))
        assert "reprendre" in result["final_response"].lower()


# =====================================================================
# render_clarification
# =====================================================================

class TestRenderClarification:
    def test_first_turn_shows_a_welcome_message(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_clarification
        c = ctx(turn_count=1)
        result = run(render_clarification(c))
        assert "Bienvenue" in result["final_response"]

    def test_later_turn_shows_examples(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_clarification
        c = ctx(turn_count=5)
        result = run(render_clarification(c))
        assert "bien saisi" in result["final_response"]

    def test_later_turn_after_a_goal_error_names_that_goal_instead_of_the_generic_examples(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_clarification
        c = ctx(turn_count=5, last_terminated_goal="PROCUREMENT_CREATE_REQUEST")
        result = run(render_clarification(c))
        assert "appel d'offres" in result["final_response"].lower()
        assert "n'a pas abouti" in result["final_response"]
        assert "bien saisi" not in result["final_response"]

    def test_the_terminated_goal_hint_is_consumed_once(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_clarification
        c = ctx(turn_count=5, last_terminated_goal="PROCUREMENT_CREATE_REQUEST")
        result = run(render_clarification(c))
        assert result["last_terminated_goal"] is None

    def test_no_terminated_goal_hint_falls_back_to_the_generic_examples(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.feedback import render_clarification
        c = ctx(turn_count=5, last_terminated_goal=None)
        result = run(render_clarification(c))
        assert "bien saisi" in result["final_response"]
        assert "last_terminated_goal" not in result


# =====================================================================
# common.py — gaps (fmt_date, unwrap_execution_result, apply_corrections, status_component)
# =====================================================================

class TestFmtDate:
    def test_none_returns_none(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import fmt_date
        assert fmt_date(None) is None
        assert fmt_date("") is None

    def test_datetime_object_is_formatted(self):
        from datetime import datetime
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import fmt_date
        assert fmt_date(datetime(2026, 12, 31)) == "31/12/2026"

    def test_iso_string_is_formatted(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import fmt_date
        assert fmt_date("2026-12-31") == "31/12/2026"

    def test_iso_string_with_z_suffix_is_formatted(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import fmt_date
        assert fmt_date("2026-12-31T10:00:00Z") == "31/12/2026"

    def test_unparseable_string_is_returned_as_is(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import fmt_date
        assert fmt_date("not-a-date") == "not-a-date"


class TestUnwrapExecutionResult:
    def test_flat_result_passes_through(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import unwrap_execution_result
        result = {"status": "success", "message": "ok"}
        assert unwrap_execution_result(result) == result

    def test_one_level_of_data_nesting_is_unwrapped(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import unwrap_execution_result
        result = {"status": "success", "data": {"status": "success", "message": "inner", "data": [1, 2]}}
        unwrapped = unwrap_execution_result(result)
        assert unwrapped["message"] == "inner"
        assert unwrapped["data"] == [1, 2]

    def test_outer_message_is_preserved_when_inner_has_none(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import unwrap_execution_result
        result = {"message": "outer msg", "data": {"status": "success", "data": []}}
        unwrapped = unwrap_execution_result(result)
        assert unwrapped["message"] == "outer msg"

    def test_non_wrapper_shaped_nested_dict_is_left_alone(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import unwrap_execution_result
        result = {"status": "success", "data": {"custom_field": "not a wrapper"}}
        unwrapped = unwrap_execution_result(result)
        assert unwrapped["data"] == {"custom_field": "not a wrapper"}

    def test_non_dict_input_returns_empty_dict(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import unwrap_execution_result
        assert unwrap_execution_result("not a dict") == {}

    def test_raw_result_string_is_parsed_and_merged(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import unwrap_execution_result
        result = {"data": {"raw_result": '{"status": "success", "message": "parsed"}'}}
        unwrapped = unwrap_execution_result(result)
        assert unwrapped.get("message") == "parsed"


class TestStatusComponent:
    def test_builds_the_expected_shape(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import status_component
        result = status_component("error", reason="x")
        assert result["kwargs"]["type"] == "error"
        assert result["kwargs"]["reason"] == "x"


class TestResolveGoalForUi:
    def test_current_goal_wins(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import resolve_goal_for_ui
        assert resolve_goal_for_ui({"current_goal": "SALES_PUBLISH_PRODUCT"}) == "SALES_PUBLISH_PRODUCT"

    def test_unknown_current_goal_falls_through_to_next_candidate(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import resolve_goal_for_ui
        state = {"current_goal": "UNKNOWN", "working_memory": {"active_goal": "BUYER_REQUEST"}}
        assert resolve_goal_for_ui(state) == "BUYER_REQUEST"

    def test_no_candidates_returns_none(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import resolve_goal_for_ui
        assert resolve_goal_for_ui({}) is None


class TestApplyCorrections:
    def test_completed_status_clears_recent_corrections_without_appending_text(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import apply_corrections
        state = {"status": "COMPLETED", "working_memory": {"recent_corrections": {"price": "300"}}}
        response = apply_corrections(state, {"final_response": "Done"})
        assert response["final_response"] == "Done"
        assert response["working_memory"]["recent_corrections"] is None

    def test_dict_shaped_corrections_are_appended_as_an_acknowledgement(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import apply_corrections
        state = {
            "status": "WAITING_INPUT",
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "working_memory": {"recent_corrections": {"price": "300 FCFA"}},
        }
        response = apply_corrections(state, {"final_response": "Recap"})
        assert "Prix" in response["final_response"]
        assert "300 FCFA" in response["final_response"]

    def test_list_shaped_corrections_are_appended(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import apply_corrections
        state = {
            "status": "WAITING_INPUT",
            "current_goal": "SALES_PUBLISH_PRODUCT",
            "working_memory": {"recent_corrections": [{"quantity": "50 kg"}]},
        }
        response = apply_corrections(state, {"final_response": "Recap"})
        assert "Quantité" in response["final_response"]

    def test_non_user_facing_field_is_ignored(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import apply_corrections
        state = {
            "status": "WAITING_INPUT",
            "working_memory": {"recent_corrections": {"internal_flag": "x"}},
        }
        response = apply_corrections(state, {"final_response": "Recap"})
        assert response["final_response"] == "Recap"

    def test_no_corrections_leaves_response_untouched(self):
        from agriconnect.graphs.agents.market_coach.nodes.rendering.common import apply_corrections
        state = {"status": "WAITING_INPUT", "working_memory": {}}
        response = apply_corrections(state, {"final_response": "Recap"})
        assert response == {"final_response": "Recap"}
