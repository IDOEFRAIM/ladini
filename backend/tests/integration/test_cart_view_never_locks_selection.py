"""Incident réel (2026-09-13, WhatsApp production, TROIS occurrences) :
"okay", puis "je suis d'accord", puis "je valide" — tous classés UNKNOWN et
retombés sur le message générique après affichage du panier.

Root cause réelle, trouvée après DEUX correctifs insuffisants (signal
`cart_pending` mal calculé, puis fast-path déterministe rejeté sur demande
produit — voir `tests/integration/test_confirmation_free_text_reliability.py
::TestCartPendingIsStateContextNeverAKeywordShortcut`) : `cart_service.py::
render_cart_menu` enveloppait l'affichage du panier dans un
`MenuRequest(kind="cart")`. Or `MenuRequest.__post_init__` documente
explicitement (mandat §25, 2026-09-09) qu'"un MenuRequest implique TOUJOURS
une sélection valide", et `nodes/ui_engine.py` (seul consommateur de
`pending_menu` dans le graphe compilé) verrouille alors
`pending_interaction=SELECTION_MENU` pour LE TOUR SUIVANT.
`interpreter/state_router.py` route toute réponse en `SELECTION_MENU` vers
le micro-prompt SELECTION (qui sait lire un index/texte de menu, jamais un
accord libre) — AVANT même que `NEW_TASK`/`cart_pending` ne soient
considérés. Aucun code, nulle part, ne lit jamais une `selection_index`/
`selected_value` contre `available_mapping_kind == "cart"` (recherche
exhaustive) : ce verrouillage ne protégeait aucune fonctionnalité réelle, il
bloquait uniquement la confirmation en texte libre que le texte du panier
invite pourtant explicitement à donner ("Répondez *précommander*...").

Fix : le panier reste un message informatif numéroté (lisibilité), jamais un
menu de sélection — `render_cart_menu` ne construit plus de `pending_menu`.
"""
from __future__ import annotations

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    InteractionKind,
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.nodes.ui_engine import ui_engine
from ladini.graphs.agents.market_coach.services.domain.cart_service import (
    CartDomainService,
)
from tests.conftest import StubRuntime, make_state, run


def _sample_cart():
    return [
        {
            "product_id": "PRD-1",
            "name": "oignons",
            "quantity": 75,
            "unit": "KG",
            "price": 225,
            "line_total": 16875,
            "vendor_name": "Mamadou",
            "source_type": "DIRECT",
        }
    ]


class TestRenderCartMenuNeverBuildsAPendingMenu:
    def test_a_non_empty_cart_has_no_pending_menu_in_its_response(self):
        cart = _sample_cart()
        meta = CartDomainService.recompute_cart_meta(cart)
        service = CartDomainService(mc_runtime=None)  # type: ignore[arg-type]
        result = service.render_cart_menu(cart, meta)
        assert "pending_menu" not in result, (
            "le panier ne doit plus jamais produire de MenuRequest — cela "
            "verrouille SELECTION_MENU pour le tour suivant et bloque toute "
            "confirmation en texte libre (voir docstring de ce fichier)"
        )
        assert "oignons" in result["final_response"]


class TestViewingTheCartNeverLocksTheNextTurnIntoSelection:
    def _view_cart_then_ui_engine(self) -> dict:
        cart = _sample_cart()
        meta = CartDomainService.recompute_cart_meta(cart)
        service = CartDomainService(mc_runtime=None)  # type: ignore[arg-type]
        cart_patch = service.render_cart_menu(cart, meta)

        state = make_state(
            current_goal="BUYER_VIEW_CART",
            active_cart=cart,
            **cart_patch,
        )
        ui_patch = run(ui_engine(state, StubRuntime()))
        merged = {**state, **ui_patch}
        return merged

    def test_pending_interaction_is_not_selection_menu_after_viewing_cart(self):
        merged = self._view_cart_then_ui_engine()
        pending = get_pending_interaction(merged)
        assert pending.kind != InteractionKind.SELECTION_MENU, (
            "afficher le panier ne doit jamais verrouiller une sélection — "
            "c'est exactement l'incident réel : plus aucune confirmation en "
            "texte libre n'était alors possible au tour suivant"
        )

    def test_the_next_turn_routes_away_from_the_selection_microprompt(self):
        merged = self._view_cart_then_ui_engine()
        pending = get_pending_interaction(merged)
        assert to_tunnel_category(pending) != "SELECTION"


def test_ui_engine_is_a_pure_pass_through_when_there_is_no_pending_menu():
    """Non-régression du contrat `ui_engine` lui-même : en l'absence de
    `pending_menu`, ce nœud ne doit RIEN muter — surtout pas
    `pending_interaction`, qu'il ne doit jamais poser de sa propre
    initiative."""
    state = make_state(current_goal="BUYER_VIEW_CART", pending_menu=None)
    patch = run(ui_engine(state, StubRuntime()))
    assert patch == {}
