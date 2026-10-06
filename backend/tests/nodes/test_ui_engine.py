"""`nodes/ui_engine.py` — point unique de conversion `MenuRequest` → composant
AG-UI. Consomme `state["pending_menu"]` et produit `ag_ui_component`,
`available_mapping`, `expected_candidates`, `working_memory` patch.

(2026-09-09, audit de clôture UI_ENGINE) : voir aussi
`tests/nodes/test_menu_contracts.py` (validation `MenuRequest`) et
`tests/unit/test_menu_snapshot_store.py` (idempotence du store)."""
from __future__ import annotations

import logging

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.flows.common.menu_contracts import (
    MenuOption,
    MenuRequest,
)
from ladini.graphs.agents.market_coach.nodes.ui_engine import (
    _resolve_menu_session_key,
    ui_engine,
)
from ladini.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
)
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


# =====================================================================
# I2 — pass-through réel : aucune mutation cachée
# =====================================================================

class TestPassThroughDoesNotMutateAnything:
    def test_no_pending_menu_creates_no_snapshot_and_no_pending_interaction(self):
        state = make_state(pending_menu=None, session_id="sess-passthrough")
        before = len(menu_snapshot_store._store)
        result = run(ui_engine(state, None))
        after = len(menu_snapshot_store._store)
        assert result == {}
        assert after == before, "un pass-through ne doit jamais créer de snapshot"
        assert get_pending_interaction({**state, **result}).kind == InteractionKind.NONE


# =====================================================================
# I3 — identité de session : jamais ""
# =====================================================================

class TestSessionIdentityResolution:
    def test_resolver_prefers_session_id_over_user_phone(self):
        state = make_state(session_id="explicit-session", user_phone="+2260")
        assert _resolve_menu_session_key(state) == "explicit-session"

    def test_resolver_falls_back_to_user_phone(self):
        state = make_state(session_id=None, user_phone="+2260")
        assert _resolve_menu_session_key(state) == "+2260"

    def test_resolver_returns_none_not_empty_string_when_both_are_absent(self):
        state = make_state(session_id=None, user_phone="")
        assert _resolve_menu_session_key(state) is None

    def test_resolver_returns_none_for_whitespace_only_identity(self):
        state = make_state(session_id="   ", user_phone=None)
        assert _resolve_menu_session_key(state) is None

    def test_no_identity_renders_the_menu_without_a_snapshot_and_logs_a_violation(
        self, caplog
    ):
        """Mandat §7 : jamais de disparition silencieuse du menu. Le menu
        reste affichable (mapping/candidates/AG-UI complets) mais SANS
        snapshot — `available_mapping` (état DURABLE) reste la source
        PRIMAIRE de résolution du tour suivant, le snapshot n'étant qu'un
        filet de secours."""
        state = make_state(pending_menu=_menu(), session_id=None, user_phone=None)
        with caplog.at_level(logging.ERROR):
            result = run(ui_engine(state, None))
        assert result["available_mapping"] == {"1": "1", "2": "2"}
        assert result["expected_candidates"] == ["Mais", "Riz"]
        assert result["ag_ui_component"]["kwargs"]["options"]
        assert "menu_snapshot_id" not in result
        assert "menu_snapshot_id" not in result["working_memory"]
        assert "menu_snapshot_id" not in result["ag_ui_component"]["kwargs"].get(
            "metadata", {}
        )
        assert any("CONTRACT VIOLATION" in r.message for r in caplog.records)
        # Le menu reste tout de même une vraie interaction de sélection.
        assert (
            get_pending_interaction({**state, **result}).kind
            == InteractionKind.SELECTION_MENU
        )


# =====================================================================
# I4 — idempotence au replay (via le VRAI store, pas un mock de save())
# =====================================================================

class TestReplaySemanticsThroughTheRealStore:
    def test_replaying_ui_engine_for_the_same_turn_reuses_the_same_snapshot(self):
        """Reproduit la sémantique réelle d'un replay LangGraph : le MÊME
        état d'entrée (même session, même turn_count, même MenuRequest)
        traverse `ui_engine` deux fois de suite — comme lors d'une reprise
        de checkpoint ou d'un retry de nœud."""
        state = make_state(
            pending_menu=_menu(), session_id="sess-replay", turn_count=7
        )
        first = run(ui_engine(dict(state), None))
        second = run(ui_engine(dict(state), None))

        assert first["menu_snapshot_id"] == second["menu_snapshot_id"]
        assert first["available_mapping"] == second["available_mapping"]
        snap1 = menu_snapshot_store.get("sess-replay", first["menu_snapshot_id"])
        snap2 = menu_snapshot_store.get("sess-replay", second["menu_snapshot_id"])
        assert snap1 is snap2

    def test_a_different_turn_in_the_same_session_gets_a_different_snapshot(self):
        state_turn_1 = make_state(pending_menu=_menu(), session_id="sess-turns", turn_count=1)
        state_turn_2 = make_state(pending_menu=_menu(), session_id="sess-turns", turn_count=2)
        r1 = run(ui_engine(state_turn_1, None))
        r2 = run(ui_engine(state_turn_2, None))
        assert r1["menu_snapshot_id"] != r2["menu_snapshot_id"]

    def test_a_genuinely_different_menu_in_the_same_turn_gets_a_different_snapshot(self):
        state_a = make_state(
            pending_menu=_menu(kind="stock"), session_id="sess-diff", turn_count=3
        )
        state_b = make_state(
            pending_menu=_menu(kind="auction"), session_id="sess-diff", turn_count=3
        )
        r1 = run(ui_engine(state_a, None))
        r2 = run(ui_engine(state_b, None))
        assert r1["menu_snapshot_id"] != r2["menu_snapshot_id"]


# =====================================================================
# I5 — snapshot ↔ mapping ↔ kind
# =====================================================================

class TestSnapshotMatchesRenderedMapping:
    def test_snapshot_mapping_and_kind_match_exactly_what_was_rendered(self):
        state = make_state(pending_menu=_menu(kind="bid"), session_id="sess-match")
        result = run(ui_engine(state, None))
        snapshot = menu_snapshot_store.get("sess-match", result["menu_snapshot_id"])
        assert snapshot.mapping == result["available_mapping"]
        assert snapshot.kind == "bid"


# =====================================================================
# I6 — menu_snapshot_id cohérent : state / working_memory / AG-UI / store
# =====================================================================

class TestMenuSnapshotIdCanonicalSourceAndSync:
    """Source canonique : `state.menu_snapshot_id` (désormais déclaré dans
    `MarketAgentState`, voir `core/state.py`). `working_memory
    ["menu_snapshot_id"]` reste un COMPATIBILITY_SHIM pour `nodes/memory.py`
    (hors périmètre de cet audit, seul lecteur réel — toujours via
    `working_memory`, jamais le top-level)."""

    def test_all_four_locations_carry_the_identical_snapshot_id(self):
        state = make_state(pending_menu=_menu(), session_id="sess-sync")
        result = run(ui_engine(state, None))
        snapshot_id = result["menu_snapshot_id"]
        assert snapshot_id
        assert result["working_memory"]["menu_snapshot_id"] == snapshot_id
        assert (
            result["ag_ui_component"]["kwargs"]["metadata"]["menu_snapshot_id"]
            == snapshot_id
        )
        stored = menu_snapshot_store.get("sess-sync", snapshot_id)
        assert stored is not None and stored.menu_id == snapshot_id

    def test_top_level_menu_snapshot_id_survives_the_compiled_graph(self):
        """Preuve P0-1-style : un champ non déclaré dans `MarketAgentState`
        est supprimé silencieusement par LangGraph à la traversée du graphe
        COMPILÉ. `menu_snapshot_id` est maintenant déclaré — ce test le
        prouve sur le VRAI schéma, pas par `dict.update`."""
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, StateGraph

        from ladini.graphs.agents.market_coach.core.state import (
            MarketAgentState,
        )

        async def writer(state):
            return {"menu_snapshot_id": "abc123"}

        workflow = StateGraph(MarketAgentState)
        workflow.add_node("writer", writer)
        workflow.set_entry_point("writer")
        workflow.add_edge("writer", END)
        graph = workflow.compile(checkpointer=MemorySaver())

        import asyncio

        result = asyncio.run(
            graph.ainvoke(
                {}, config={"configurable": {"thread_id": "t-menu-snapshot-survival"}}
            )
        )
        assert result.get("menu_snapshot_id") == "abc123"


# =====================================================================
# I7 — consommation uniquement après matérialisation réussie
# =====================================================================

class TestMenuIsConsumedOnlyAfterSuccessfulMaterialization:
    def test_snapshot_store_failure_still_serves_the_menu_and_still_consumes_it(
        self, monkeypatch, caplog
    ):
        """Le contrat de sélection (mapping/candidates/AG-UI/
        pending_interaction) est déjà entièrement construit AVANT l'appel
        au store — une panne du store dégrade (pas de snapshot) mais ne
        fait JAMAIS perdre la demande de menu elle-même."""
        def _boom(*args, **kwargs):
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(menu_snapshot_store, "save", _boom)
        state = make_state(pending_menu=_menu(), session_id="sess-boom")
        with caplog.at_level(logging.ERROR):
            result = run(ui_engine(state, None))

        assert result["available_mapping"] == {"1": "1", "2": "2"}
        assert result["pending_menu"] is None, "le menu doit être consommé (servi avec succès)"
        assert "menu_snapshot_id" not in result
        assert any(
            "menu_snapshot_store.save a échoué" in r.message for r in caplog.records
        )
        assert any(r.levelno == logging.ERROR for r in caplog.records)


# =====================================================================
# I8 — aucune décision métier (rôle, goal)
# =====================================================================

class TestNoBusinessDecisionInUiEngine:
    def test_pending_interaction_goal_is_never_set(self):
        """Mandat §12-14 : recherche exhaustive faite, `PendingInteraction.goal`
        n'a AUCUN lecteur réel dans `src/ladini` — ui_engine ne
        l'invente pas. L'identification du menu passe par `context_ref` +
        `menu_snapshot_id` + `available_mapping_kind`."""
        state = make_state(pending_menu=_menu(), current_goal="SALES_PUBLISH_PRODUCT")
        result = run(ui_engine(state, None))
        pending = get_pending_interaction({**state, **result})
        assert pending.kind == InteractionKind.SELECTION_MENU
        assert pending.goal is None

    def test_rendering_is_identical_regardless_of_user_role(self):
        """Le rôle de profil ne doit JAMAIS filtrer/altérer un menu déjà
        décidé par le flow — MenuRequest est la décision métier, ui_engine
        ne fait qu'exécuter."""
        menu_producer = _menu()
        menu_buyer = _menu()
        state_producer = make_state(pending_menu=menu_producer, user_role="PRODUCER")
        state_buyer = make_state(pending_menu=menu_buyer, user_role="BUYER")
        result_producer = run(ui_engine(state_producer, None))
        result_buyer = run(ui_engine(state_buyer, None))
        assert result_producer["available_mapping"] == result_buyer["available_mapping"]
        assert result_producer["expected_candidates"] == result_buyer["expected_candidates"]
        assert (
            result_producer["ag_ui_component"]["kwargs"]["options"]
            == result_buyer["ag_ui_component"]["kwargs"]["options"]
        )

    def test_module_contains_no_role_based_branching(self):
        import inspect

        import ladini.graphs.agents.market_coach.nodes.ui_engine as mod

        source = inspect.getsource(mod)
        assert "BUYER" not in source
        assert "PRODUCER" not in source
        assert "user_role" not in source
        assert "role_up" not in source


# =====================================================================
# Mandat §23 : `_build_ag_ui_component` est une fonction PURE
# =====================================================================

class TestBuildAgUiComponentIsPure:
    def test_pure_function_never_touches_the_snapshot_store(self):
        from ladini.graphs.agents.market_coach.nodes.ui_engine import (
            _build_ag_ui_component,
        )

        before = dict(menu_snapshot_store._store)
        component = _build_ag_ui_component(_menu())
        after = dict(menu_snapshot_store._store)
        assert before == after
        assert component["id"] == ["ag_ui", "ListMenu"]


# =====================================================================
# Mandat §37 : pas de rebouclage accidentel dans le graphe compilé
# =====================================================================

class TestCompiledGraphNeverLoopsBackFromUiEngine:
    def test_ui_engine_has_a_single_fixed_downstream_edge_to_response_strategy(self):
        from ladini.graphs.agents.market_coach.core.graph_builder import (
            build_graph,
        )

        graph = build_graph(role="PRODUCER", mc_runtime=object()).get_graph()
        outgoing = {(e.source, e.target, e.data) for e in graph.edges if e.source == "ui_engine"}
        assert outgoing == {("ui_engine", "response_strategy", None)}

    def test_response_strategy_downstream_never_reaches_a_pending_menu_producer_again(self):
        """Un tour est un DAG : une fois `ui_engine` traversé, on ne doit
        plus jamais retomber sur un producteur de `pending_menu`
        (`cart_management`, `negotiation_gate`, `order_tracking_node`,
        `context_resolver`, `ensure_farm_node`) avant `END` — sinon un menu
        pourrait être régénéré/écrasé dans le MÊME tour."""
        from ladini.graphs.agents.market_coach.core.graph_builder import (
            build_graph,
        )

        graph = build_graph(role="PRODUCER", mc_runtime=object()).get_graph()
        adjacency: dict[str, set[str]] = {}
        for e in graph.edges:
            adjacency.setdefault(e.source, set()).add(e.target)

        seen, stack = set(), ["response_strategy"]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(adjacency.get(node, ()))

        producers = {
            "cart_management",
            "negotiation_gate",
            "order_tracking_node",
            "context_resolver",
            "ensure_farm_node",
        }
        assert not (seen & producers), (
            f"response_strategy peut re-atteindre un producteur de menu : {seen & producers}"
        )
