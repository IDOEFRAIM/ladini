"""`nodes/ui_engine.py` — point unique de conversion `MenuRequest` → composant
AG-UI. Consomme `state["pending_menu"]` et produit `ag_ui_component`,
`available_mapping`, `expected_candidates`, `working_memory` patch."""
from __future__ import annotations

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from agriconnect.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from agriconnect.graphs.agents.market_coach.nodes.ui_engine import ui_engine
from tests.conftest import make_state, run


def _menu(**overrides):
    defaults = dict(
        title="Choisissez",
        options=[MenuOption(index="1", label="Mais"), MenuOption(index="2", label="Riz")],
        kind="stock",
    )
    defaults.update(overrides)
    return MenuRequest(**defaults)


class TestUiEngine:
    def test_no_pending_menu_is_a_silent_pass_through(self):
        state = make_state(pending_menu=None)
        result = run(ui_engine(state, None))
        assert result == {}

    def test_non_menu_request_value_is_also_a_pass_through(self):
        state = make_state(pending_menu={"not": "a MenuRequest"})
        result = run(ui_engine(state, None))
        assert result == {}

    def test_menu_produces_mapping_and_candidates(self):
        state = make_state(pending_menu=_menu())
        result = run(ui_engine(state, None))
        assert result["available_mapping"] == {"1": "1", "2": "2"}
        assert result["expected_candidates"] == ["Mais", "Riz"]
        assert (
            get_pending_interaction({**state, **result}).kind
            == InteractionKind.SELECTION_MENU
        )

    def test_option_with_explicit_value_overrides_index_in_mapping(self):
        menu = _menu(options=[MenuOption(index="1", label="Mais", value="stock-uuid-1")])
        state = make_state(pending_menu=menu)
        result = run(ui_engine(state, None))
        assert result["available_mapping"] == {"1": "stock-uuid-1"}

    def test_working_memory_is_merged_not_replaced(self):
        state = make_state(pending_menu=_menu(), working_memory={"existing_key": "kept"})
        result = run(ui_engine(state, None))
        assert result["working_memory"]["existing_key"] == "kept"
        assert result["working_memory"]["available_mapping_kind"] == "stock"

    def test_pending_menu_is_consumed(self):
        state = make_state(pending_menu=_menu())
        result = run(ui_engine(state, None))
        assert result["pending_menu"] is None

    def test_ag_ui_component_shape_and_kind_metadata(self):
        state = make_state(pending_menu=_menu(kind="bid"))
        result = run(ui_engine(state, None))
        component = result["ag_ui_component"]
        assert component["id"] == ["ag_ui", "ListMenu"]
        assert component["kwargs"]["metadata"]["kind"] == "bid"
        assert len(component["kwargs"]["options"]) == 2

    def test_menu_snapshot_id_is_attached_to_component_and_working_memory(self):
        state = make_state(pending_menu=_menu(), session_id="sess1")
        result = run(ui_engine(state, None))
        assert result["menu_snapshot_id"]
        assert result["working_memory"]["menu_snapshot_id"] == result["menu_snapshot_id"]
        assert result["ag_ui_component"]["kwargs"]["metadata"]["menu_snapshot_id"] == result["menu_snapshot_id"]

    def test_session_id_falls_back_to_user_phone(self):
        state = make_state(pending_menu=_menu(), session_id=None, user_phone="+2260")
        result = run(ui_engine(state, None))
        assert result["menu_snapshot_id"]

    def test_preformatted_text_becomes_final_response_when_no_base_text(self):
        state = make_state(pending_menu=_menu(preformatted_text="Voici le menu preformate"), final_response=None)
        result = run(ui_engine(state, None))
        assert result["final_response"] == "Voici le menu preformate"

    def test_preformatted_text_is_appended_to_existing_base_text(self):
        state = make_state(
            pending_menu=_menu(preformatted_text="Instructions ici"),
            final_response="Texte de base",
        )
        result = run(ui_engine(state, None))
        assert result["final_response"] == "Texte de base\n\nInstructions ici"

    def test_identical_base_text_and_instructions_are_not_duplicated(self):
        state = make_state(
            pending_menu=_menu(preformatted_text="Meme texte"),
            final_response="Meme texte",
        )
        result = run(ui_engine(state, None))
        assert result["final_response"] == "Meme texte"

    def test_base_text_preserved_when_menu_has_no_preformatted_text(self):
        state = make_state(pending_menu=_menu(preformatted_text=None), final_response="Texte existant")
        result = run(ui_engine(state, None))
        assert result["final_response"] == "Texte existant"

    def test_no_base_text_and_no_preformatted_text_omits_final_response(self):
        state = make_state(pending_menu=_menu(preformatted_text=None), final_response=None)
        result = run(ui_engine(state, None))
        assert "final_response" not in result

    def test_extra_kwargs_are_accepted_for_safe_node_compatibility(self):
        state = make_state(pending_menu=None)
        result = run(ui_engine(state, None, some_extra_kwarg="ignored"))
        assert result == {}
