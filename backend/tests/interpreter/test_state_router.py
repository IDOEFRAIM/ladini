"""Chantier "State Router + micro-prompts" — INCRÉMENT A (2026-09-12).

Couvre `InterpretationRoute`/`choose_interpretation_route`
(`interpreter/state_router.py`) : une fonction PURE, déterministe sur
l'état, qui ne fait ni appel LLM ni analyse linguistique ni filtrage par
`user_role` — voir le contrat exact dans la docstring du module.

Ce fichier ne teste QUE le router lui-même (Incrément A). Les
micro-prompts SELECTION/ACTIVE_SLOT/STRUCTURED_ACTION eux-mêmes sont hors
scope (incréments B/C/D séparés) — le router n'est pas encore câblé dans
le graphe de production, il reste un composant testé en isolation."""
from __future__ import annotations

from ladini.graphs.agents.market_coach.interpreter.state_router import (
    InterpretationRoute,
    choose_interpretation_route,
)
from tests.conftest import make_state


def _vendor_list_state(**overrides):
    """État minimal où le tunnel producteur est actif — 2 producteurs,
    aucun choisi encore → `build_selection_context` renvoie
    `expected_action=SELECT_PRODUCER` (voir `domain/selection_actions.py`)."""
    base = make_state(**overrides)
    base["vendor_selection_context"] = {
        "vendors": [
            {"producer_id": "p1", "vendor_name": "Diallo", "price": 250, "unit": "KG"},
            {"producer_id": "p2", "vendor_name": "Ouedraogo", "price": 240, "unit": "KG"},
        ]
    }
    return base


class TestNoActiveTunnelRoutesToNewTask:
    def test_completely_empty_state_is_new_task(self):
        assert choose_interpretation_route({}) == InterpretationRoute.NEW_TASK

    def test_default_make_state_is_new_task(self):
        state = make_state()
        assert choose_interpretation_route(state) == InterpretationRoute.NEW_TASK

    def test_a_goal_alone_without_any_expected_slot_is_still_new_task(self):
        # `current_goal` seul ne suffit pas : il faut AUSSI un slot attendu
        # (spec §5, route ACTIVE_SLOT) — sinon un goal résiduel routerait à
        # tort vers ACTIVE_SLOT alors qu'aucune question n'est en attente.
        state = make_state(current_goal="SALES_PUBLISH_PRODUCT", expected_input="NONE")
        assert choose_interpretation_route(state) == InterpretationRoute.NEW_TASK


class TestSelectionMenuActive:
    def test_generic_selection_menu_routes_to_selection(self):
        state = make_state(expected_input="SELECTION")
        assert choose_interpretation_route(state) == InterpretationRoute.SELECTION

    def test_selection_route_does_not_require_a_current_goal(self):
        # Un menu de commandes (order tracking) peut être actif SANS
        # `current_goal` posé — la route ne doit pas en dépendre.
        state = make_state(expected_input="SELECTION", current_goal=None)
        assert choose_interpretation_route(state) == InterpretationRoute.SELECTION


class TestActiveSlot:
    def test_price_slot_with_active_goal_routes_to_active_slot(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT", expected_input="PRICE"
        )
        assert choose_interpretation_route(state) == InterpretationRoute.ACTIVE_SLOT

    def test_quantity_slot_with_active_goal_routes_to_active_slot(self):
        state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT", expected_input="QUANTITY"
        )
        assert choose_interpretation_route(state) == InterpretationRoute.ACTIVE_SLOT

    def test_farm_name_slot_with_active_goal_routes_to_active_slot(self):
        state = make_state(
            current_goal="ONBOARDING_PRODUCER", expected_input="FARM_NAME"
        )
        assert choose_interpretation_route(state) == InterpretationRoute.ACTIVE_SLOT

    def test_date_slot_with_active_goal_routes_to_active_slot(self):
        state = make_state(current_goal="SALES_PUBLISH_PRODUCT", expected_input="DATE")
        assert choose_interpretation_route(state) == InterpretationRoute.ACTIVE_SLOT


class TestSubflowOwnedInteractionsAreNeverActiveSlot:
    """Phase C (2026-09-12) : CONFIRM_ACTION/PROVIDE_LOCATION/VERIFY_OTP sont
    possédés par leur propre sous-flux dédié (`confirmation_gate`/
    `gps_delivery_gate`/le flux OTP producteur — voir
    `core/pending_interaction.py::SUBFLOW_OWNED_KINDS`) — jamais de simples
    champs texte à extraire par un micro-prompt générique. Corrige un
    classement erroné de l'Incrément A (ces catégories y étaient traitées
    comme ACTIVE_SLOT faute d'avoir encore dérivé la règle de
    `SLOT_FILLING_INPUTS` — sans conséquence à l'époque puisque ACTIVE_SLOT
    n'était pas encore câblé à un comportement réel)."""

    def test_confirmation_with_active_goal_does_not_route_to_active_slot(self):
        state = make_state(
            current_goal="PROCUREMENT_CREATE_REQUEST", expected_input="CONFIRMATION"
        )
        assert choose_interpretation_route(state) != InterpretationRoute.ACTIVE_SLOT
        assert choose_interpretation_route(state) == InterpretationRoute.NEW_TASK

    def test_otp_with_active_goal_does_not_route_to_active_slot(self):
        state = make_state(current_goal="SALES_PUBLISH_PRODUCT", expected_input="OTP")
        assert choose_interpretation_route(state) != InterpretationRoute.ACTIVE_SLOT
        assert choose_interpretation_route(state) == InterpretationRoute.NEW_TASK

    def test_provide_location_with_active_goal_does_not_route_to_active_slot(self):
        # `PROVIDE_LOCATION` résout à la MÊME catégorie "LOCATION" qu'un
        # simple champ `zone` (ENTER_FIELD) via `to_tunnel_category` — c'est
        # précisément le cas qui exige de discriminer par `pending.kind`,
        # pas seulement par catégorie de chaîne.
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )

        state = make_state(current_goal="BUYER_REQUEST")
        state.update(set_pending_interaction(InteractionKind.PROVIDE_LOCATION))
        assert choose_interpretation_route(state) != InterpretationRoute.ACTIVE_SLOT
        assert choose_interpretation_route(state) == InterpretationRoute.NEW_TASK

    def test_a_genuine_zone_field_slot_still_routes_to_active_slot(self):
        # Contrepreuve : le VRAI champ `zone` (ENTER_FIELD, pas
        # PROVIDE_LOCATION) doit lui rester ACTIVE_SLOT — la distinction
        # porte sur le kind, pas sur le fait que la catégorie "LOCATION"
        # soit exclue en bloc.
        state = make_state(current_goal="ONBOARDING_PRODUCER", expected_input="LOCATION")
        assert choose_interpretation_route(state) == InterpretationRoute.ACTIVE_SLOT


class TestStructuredActionHasTopPriority:
    def test_active_vendor_selection_tunnel_routes_to_structured_action(self):
        state = _vendor_list_state()
        assert (
            choose_interpretation_route(state)
            == InterpretationRoute.STRUCTURED_ACTION
        )

    def test_structured_action_wins_even_when_a_goal_and_slot_are_also_set(self):
        # Incident réel de référence (2026-09-01, "LE PREMIER, C'EST À DIRE
        # 5 L") : le tunnel producteur/palier doit toujours primer sur un
        # slot générique par ailleurs actif dans le même état.
        state = _vendor_list_state(
            current_goal="PROCUREMENT_CREATE_REQUEST", expected_input="QUANTITY"
        )
        assert (
            choose_interpretation_route(state)
            == InterpretationRoute.STRUCTURED_ACTION
        )

    def test_structured_action_wins_over_a_generic_selection_menu(self):
        state = _vendor_list_state(expected_input="SELECTION")
        assert (
            choose_interpretation_route(state)
            == InterpretationRoute.STRUCTURED_ACTION
        )

    def test_a_reset_vendor_context_does_not_trigger_structured_action(self):
        # `_live_ctx` (selection_actions.py) ignore un contexte marqué
        # `__reset__` — un résidu de tour précédent ne doit pas rouvrir le
        # tunnel structuré à tort.
        state = make_state()
        state["vendor_selection_context"] = {"__reset__": True, "vendors": []}
        assert choose_interpretation_route(state) == InterpretationRoute.NEW_TASK


class TestPureFunctionContract:
    def test_route_is_identical_for_buyer_and_producer_given_the_same_tunnel_state(self):
        # Invariant absolu du chantier (§0.1) : le router ne doit JAMAIS
        # dépendre de `user_role` — le double rôle acheteur/producteur doit
        # rester intact à ce niveau comme partout ailleurs.
        buyer_state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRICE",
            user_role="BUYER",
        )
        producer_state = make_state(
            current_goal="SALES_PUBLISH_PRODUCT",
            expected_input="PRICE",
            user_role="PRODUCER",
        )
        assert choose_interpretation_route(buyer_state) == choose_interpretation_route(
            producer_state
        )

    def test_calling_the_router_does_not_mutate_the_input_state(self):
        state = make_state(current_goal="SALES_PUBLISH_PRODUCT", expected_input="PRICE")
        snapshot = dict(state)
        choose_interpretation_route(state)
        assert state == snapshot

    def test_the_message_text_has_no_influence_on_the_route(self):
        # Le router ne doit JAMAIS lire `normalized_text`/`user_query` —
        # seule la classification linguistique (future micro-prompt) en a
        # le droit, jamais le routage d'état.
        base = make_state(current_goal="SALES_PUBLISH_PRODUCT", expected_input="PRICE")
        state_a = dict(base, normalized_text="250 francs le kilo")
        state_b = dict(base, normalized_text="je cherche des poulets")
        assert choose_interpretation_route(state_a) == choose_interpretation_route(
            state_b
        )


class TestActiveSlotClassificationIsDerivedNotCopied:
    """Phase C §23 : preuve structurelle qu'ACTIVE_SLOT dérive de
    `core/slots.py::SLOT_FILLING_INPUTS` — jamais une liste littérale
    ``{"PRODUCT", "PRICE", "QUANTITY", ...}`` recopiée dans ce module."""

    def test_every_slot_filling_category_routes_to_active_slot(self):
        from ladini.graphs.agents.market_coach.core.slots import (
            SLOT_FILLING_INPUTS,
        )

        for category in SLOT_FILLING_INPUTS:
            state = make_state(current_goal="SALES_PUBLISH_PRODUCT", expected_input=category)
            assert choose_interpretation_route(state) == InterpretationRoute.ACTIVE_SLOT, (
                f"catégorie {category!r} (membre de SLOT_FILLING_INPUTS) "
                "devrait router vers ACTIVE_SLOT"
            )

    def test_state_router_module_does_not_hardcode_a_slot_category_literal_set(self):
        # Garde structurel : le module source ne doit contenir aucune
        # collection littérale de catégories de slot (ex: un `frozenset`
        # local {"PRODUCT", "PRICE", ...}) — seul un IMPORT de
        # `SLOT_FILLING_INPUTS` est autorisé à porter cette information.
        import inspect

        from ladini.graphs.agents.market_coach.interpreter import state_router

        source = inspect.getsource(state_router)
        assert "SLOT_FILLING_INPUTS" in source
        for forbidden_literal in ('"PRODUCT"', '"PRICE"', '"QUANTITY"'):
            assert forbidden_literal not in source, (
                f"{forbidden_literal} ne doit apparaître nulle part dans "
                "state_router.py — seule SLOT_FILLING_INPUTS fait foi"
            )


class TestNoDurableActiveSlotStateWasIntroduced:
    """Phase C §26 : ACTIVE_SLOT est une PROJECTION de `PendingInteraction`
    à chaque tour — jamais un second canal d'état persistant concurrent."""

    def test_market_agent_state_has_no_active_slot_field(self):
        from ladini.graphs.agents.market_coach.core.state import MarketAgentState

        assert "active_slot" not in MarketAgentState.__annotations__

    def test_choosing_the_route_never_mutates_or_writes_to_the_state(self):
        state = make_state(current_goal="SALES_PUBLISH_PRODUCT", expected_input="PRICE")
        snapshot = dict(state)
        choose_interpretation_route(state)
        assert state == snapshot
        assert "active_slot" not in state


class TestStateRouterNeverImportsTheIntentCatalog:
    def test_module_source_does_not_reference_the_intent_catalog_or_llm_gateway(self):
        # Garde structurel (spec §53) : le router décide uniquement à
        # partir de l'état — il n'a aucune raison légitime d'importer le
        # catalogue des 41 intentions, les prompts, ou le gateway LLM. Une
        # future dérive qui lui ferait importer `interpreter.intent`/
        # `interpreter.prompts`/`llm_gateway` romprait le principe "PYTHON
        # connaît l'état, ne comprend PAS le langage" — ce test la détecte
        # sans dépendre de l'ordre d'import runtime (source statique).
        import inspect

        from ladini.graphs.agents.market_coach.interpreter import state_router

        source = inspect.getsource(state_router)
        for forbidden in ("interpreter.intent", "interpreter.prompts", "llm_gateway"):
            assert forbidden not in source, f"state_router.py ne doit pas référencer {forbidden}"


class TestInterpretationRouteEnum:
    def test_all_four_target_routes_exist(self):
        values = {r.value for r in InterpretationRoute}
        assert {"new_task", "active_slot", "selection", "structured_action"} <= values

    def test_unified_fallback_exists_for_migration_but_is_never_returned_yet(self):
        # Conservé pendant la migration (spec §3) : présent dans l'enum,
        # mais `choose_interpretation_route` ne le renvoie encore jamais —
        # aucun câblage de repli n'existe tant que B/C/D ne sont pas livrés.
        assert InterpretationRoute.UNIFIED_FALLBACK.value == "unified_fallback"
        for state in ({}, make_state(), make_state(expected_input="SELECTION")):
            assert (
                choose_interpretation_route(state)
                != InterpretationRoute.UNIFIED_FALLBACK
            )

    def test_route_is_a_str_enum_serializable_for_langfuse_metadata(self):
        # Doit pouvoir être déposé tel quel dans `extra_metadata` (spec
        # §44 : `interpretation_route` en télémétrie) sans conversion
        # manuelle — `str(route) == route.value` via `str, Enum`.
        assert InterpretationRoute.SELECTION == "selection"
        assert isinstance(InterpretationRoute.SELECTION.value, str)
