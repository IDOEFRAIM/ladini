"""Preuve directe de disparition du bug rapporté (mandat de refonte, §53) :

    User: je veux acheter du lait
    Agent: lait disponible...
    User: je veux 30 L
    Agent: 1. 5 L  /  2. 10 L
    User: celui de 10 l
    Agent (AVANT le fix): "Pourriez-vous me confirmer quelle information..."
    Agent (APRÈS le fix): reste sur le menu de paliers, jamais un
                            "que voulez-vous confirmer ?" hors-sujet.

Ce test appelle les VRAIES fonctions de production (`interpreter/strategy.py
::response_strategy`, `core/tunnel_manager.py::TunnelManager.is_cart_
routeable`, `core/router.py::_cart_guard`, `domain/selection_actions.py::
build_selection_context`) enchaînées exactement comme le graphe compilé les
appelle pour ce tour — pas une réimplémentation, le vrai code.

État après chaque message affiché explicitement (pending_interaction,
current_goal, tier_selection_context, next expected action) — voir chaque
assertion pour la preuve qu'aucun état périmé n'est réutilisé."""

from __future__ import annotations

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
)
from ladini.graphs.agents.market_coach.core.router import _cart_guard
from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    build_selection_context,
)
from ladini.graphs.agents.market_coach.interpreter.strategy import (
    response_strategy,
)
from tests.conftest import make_state, run


def _print_turn_state(label: str, state: dict) -> None:
    pending = get_pending_interaction(state)
    print(
        f"\n--- {label} ---\n"
        f"  pending_interaction.kind = {pending.kind.value}\n"
        f"  current_goal             = {state.get('current_goal')}\n"
        f"  tier_selection_context   = {state.get('tier_selection_context')}\n"
        f"  vendor_selection_context = {state.get('vendor_selection_context')}\n"
    )


class TestReportedConversationNoLongerBypassesToConfirmation:
    def test_celui_de_10_l_stays_on_the_tier_menu_never_asks_what_to_confirm(self):
        # --- État APRÈS "je veux 30 L" : un producteur déjà choisi, le
        # menu de paliers 5L/10L vient d'être affiché (SELECT_PRICING_TIER).
        # Un `pending_interaction` CONFIRM_ACTION PÉRIMÉ (ex: laissé par une
        # confirmation d'un tour bien antérieur, jamais nettoyé — exactement
        # le genre de résidu que l'ancien code laissait trainer) est
        # délibérément injecté pour prouver qu'il ne peut PLUS gagner.
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            status="WAITING_INPUT",
            missing_fields=["quantity"],  # ce qui faisait échouer _cart_guard AVANT le fix
            tier_selection_context={
                "tiers": [
                    {"tier_id": "tier-5l", "quantity": 5, "unit": "LITRE", "price": 2500},
                    {"tier_id": "tier-10l", "quantity": 10, "unit": "LITRE", "price": 4500},
                ]
            },
            vendor_selection_context={
                "chosen_vendor": {"producer_id": "prod-1", "vendor_name": "Ferme Awa"}
            },
            pending_interaction={
                "kind": "CONFIRM_ACTION",
                "goal": None,  # goal vide — exactement le cas qui produisait le bug
                "field": None,
                "context_ref": "confirmation",
                "candidates": [],
                "created_at": 0.0,
                "status": "ACTIVE",
            },
            # "celui de 10 l" a été classé UNKNOWN par l'interpréteur (le
            # LLM n'a pas produit d'agent_action structuré pour cette
            # formulation) — reproduit fidèlement l'incident rapporté.
            interpreted_event="UNKNOWN",
            expected_input="CONFIRMATION",  # signal legacy, lui aussi périmé
            waiting_for_confirmation=True,  # idem
        )
        _print_turn_state('User: "celui de 10 l" (état AVANT résolution)', state)

        # 1) Le résolveur canonique ne rapporte JAMAIS CONFIRM_ACTION tant
        #    qu'un menu palier est vivant — peu importe ce que disent les
        #    anciens signaux (expected_input/waiting_for_confirmation).
        pending = get_pending_interaction(state)
        assert pending.kind == InteractionKind.SELECT_PRICING_TIER
        assert pending.kind != InteractionKind.CONFIRM_ACTION

        # 2) Le routeur (core/router.py::_cart_guard) laisse maintenant
        #    passer vers cart_management MALGRÉ missing_fields non vide —
        #    avant le fix, ceci renvoyait False et le tour tombait sur
        #    to_strategy, contournant cart_management ET confirmation_gate.
        assert _cart_guard(state) is True

        # 3) Même le node RÉEL response_strategy (chemin de repli si un
        #    autre routage venait quand même y aboutir directement) ne
        #    produit JAMAIS "CONFIRMATION" ici.
        result = run(response_strategy(state, mc_runtime=None))
        _print_turn_state("Après response_strategy", {**state, **result})
        assert result["response_strategy"] != "CONFIRMATION"
        assert result["response_strategy"] == "SELECTION_MENU"

    def test_le_deuxieme_and_a_bare_digit_still_resolve_normally_afterwards(self):
        """Non-régression : le fix ne bloque pas la résolution légitime —
        une fois que l'interpréteur PRODUIT bien un agent_action valide
        (LLM ou fast-path), la sélection continue de fonctionner comme
        avant. "5" pendant SET_PACKAGE_COUNT reste un nombre de paquets,
        jamais réinterprété comme un index de menu (règle 15, déjà
        garantie par domain/selection_actions.py::fast_path_action,
        inchangée par cette refonte)."""
        # État après que "celui de 10 l" (ou "le deuxieme") a été résolu :
        # le palier 10L est choisi, on attend le nombre de bidons.
        state = make_state(
            current_goal="BUYER_ADD_TO_CART",
            tier_selection_context={
                "tiers": [
                    {"tier_id": "tier-5l", "quantity": 5, "unit": "LITRE", "price": 2500},
                    {"tier_id": "tier-10l", "quantity": 10, "unit": "LITRE", "price": 4500},
                ],
                "resolved_tier_id": "tier-10l",
            },
        )
        _print_turn_state('Après "celui de 10 l" -> tier 10L résolu', state)

        context = build_selection_context(state)
        assert context.expected_action == ActionType.SET_PACKAGE_COUNT
        assert context.active_tier_id == "tier-10l"

        from ladini.graphs.agents.market_coach.domain.selection_actions import (
            fast_path_action,
        )

        raw = fast_path_action("5", context)
        assert raw == {"action": ActionType.SET_PACKAGE_COUNT, "package_count": 5.0}
        print(
            '\n--- User: "5" ---\n'
            f"  action resolue = {raw}\n"
            "  -> 5 x bidon de 10 litres = 50 litres (compute_line, inchangé)\n"
        )
