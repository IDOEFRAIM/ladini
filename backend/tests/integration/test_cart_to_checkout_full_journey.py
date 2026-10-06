"""TEST DE PARCOURS COMPLET — MESSAGE → ... → EXÉCUTION (2026-09-04, clôture
frontière CART→CHECKOUT→PREORDER).

Chaîne RÉELLEMENT traversée, aucune étape simulée en appelant une fonction
domain isolément :

    "je veux 30 L de lait" (tour 1, palier PAS encore choisi)
      → cart_management (RÉEL) — menu de paliers
    "2" (tour 2)
      → cart_management (RÉEL) — palier résolu, quantité pré-palier PURGÉE,
        demande le nombre de paquets
    "3" (tour 3)
      → cart_management (RÉEL) — add_to_cart_with_ref (RÉEL) : 3 bidons de
        10 L, 900 FCFA/bidon = 2700 FCFA, 30 L au total
      → active_cart (RÉEL état LangGraph, reducers RÉELS via `apply_patch`)
    "confirmer" (tour 4)
      → DomainRouter.decide (RÉEL) → create_preorder (RÉEL nœud d'entrée)
      → bootstrap_preorder_draft (RÉEL) → RecordingRuntime en frontière
        `mc_runtime.call_db` (même frontière que `services/mcp/gateway.py`)
      → GPS inconnu → NEEDS_LOCATION
    position GPS partagée (tour 5)
      → resolve_preorder_confirmation (RÉEL) → apply_domain_action (RÉEL) →
        CAS PostgreSQL (faux moteur SQL, mêmes fixtures que
        `test_preorder_draft_persistence.py`) → confirm_preorder_draft (RÉEL
        appel MCP, canned) → EXECUTED

Le test verrouille EXACTEMENT ce que le mandat demande (§19, reproduction de
l'incident historique) : `tier_id`/`base_unit_quantity`/`package_count`/
`price`/`line_total` doivent porter la MÊME valeur à chaque étape — panier
affiché, snapshot serveur checkout, `PreorderDraft` confirmé — jamais une
divergence entre affichage et exécution.

## Portée honnête (identique aux autres tests de cette suite)

Comme `test_user_journey_procurement_preorder.py` (dont ce fichier reprend
directement le gabarit) : l'interpréteur LLM n'est PAS exercé — chaque tour
part d'un `transaction_payload`/`extracted_entities` déjà "interprété".
`RecordingRuntime` REMPLACE le serveur MCP réel : les valeurs qu'il renvoie
pour `create_preorder_draft`/`confirm_preorder_draft` sont ÉCRITES PAR CE
TEST pour reproduire ce qu'un serveur correctement résolu renverrait — la
résolution palier RÉELLE (`resolve_tier`/`compute_line` côté serveur) est
verrouillée séparément par `tests/unit/test_create_preorder_draft_pricing_tiers.py`,
pas ré-exercée ici. Ce que CE fichier apporte : la PLOMBERIE bout en bout —
que `tier_id` survive intact du panier jusqu'au draft confirmé, à travers
tous les nœuds réels et tous les reducers réels."""
from __future__ import annotations

from typing import Any, Dict

from ladini.core.settings import settings
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.router import DomainRouter
from ladini.graphs.agents.market_coach.domain.preorder_draft import (
    PreorderDraftStatus,
)
from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
from ladini.graphs.agents.market_coach.flows.buyer.preorder import create_preorder
from ladini.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from ladini.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from ladini.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from ladini.graphs.agents.market_coach.nodes.memory import memory_update
from ladini.graphs.agents.market_coach.nodes.validation import validator
from ladini.services.database import preorder_draft_store
from tests.architecture.test_preorder_draft_persistence import (
    _install_fake_db as _install_fake_preorder_db,
)
from tests.conftest import StubRuntime, run
from tests.evals.runners.harness import RecordingRuntime
from tests.integration.test_tier_selection_full_node_chain import apply_patch

TIERS = [
    {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 500.0,
     "packaging": "bidon", "base_unit_quantity": 5.0, "min_order_quantity": 1},
    {"tier_id": "t10", "quantity": 10.0, "unit": "L", "price": 900.0,
     "packaging": "bidon", "base_unit_quantity": 10.0, "min_order_quantity": 1},
]


def _tiered_vendor() -> Dict[str, Any]:
    return {
        "product_id": "P-LAIT", "name": "lait", "price": 500.0, "unit": "LITRE",
        "vendor_name": "jojo", "producer_id": "PR-JOJO", "source_type": "DIRECT",
        "is_auction": False, "pricing_tiers": [dict(t) for t in TIERS],
    }


def _patch_vendors(monkeypatch, vendor: Dict[str, Any]) -> None:
    import ladini.graphs.agents.market_coach.services.domain.cart_service as m

    async def _fake(self, phone, product_name):
        return [vendor], False

    monkeypatch.setattr(m.CartDomainService, "resolve_product_vendors", _fake)


def _cart_runtime() -> StubRuntime:
    return StubRuntime(
        responses={
            "validate_stock_availability_atomic": {
                "status": "success",
                "available_quantity": 500.0,
                "unit": "LITRE",
                "unit_price": 500.0,
            }
        }
    )


def _turn_boundary(state: Dict[str, Any], runtime: Any) -> Dict[str, Any]:
    state = apply_patch(state, run(state_cleaner_node(state, runtime)))
    return apply_patch(state, run(post_response_cleanup(state, runtime)))


def _say(state, runtime, interpreter, text: str) -> Dict[str, Any]:
    state = dict(state)
    state["normalized_text"] = text
    state["user_query"] = text
    state = apply_patch(state, run(interpreter(state, runtime)))
    state = apply_patch(state, run(memory_update(state, runtime)))
    state = apply_patch(state, run(validator(state, runtime)))
    state = apply_patch(state, run(cart_management(state, runtime)))
    return state


def _open(runtime, text: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "current_goal": "BUYER_ADD_TO_CART", "expected_input": "NONE",
        "status": "PROCESSING", "normalized_text": text, "user_query": text,
        "user_phone": "+22670000099", "transaction_payload": payload,
        "working_memory": {}, "active_cart": [],
    }


class TestFullBuyerJourneyMessageToExecution:
    def test_30l_upfront_then_tier_10l_then_3_packages_survives_unchanged_through_checkout_and_confirm(
        self, monkeypatch
    ):
        _install_fake_preorder_db(monkeypatch)
        monkeypatch.setattr(settings, "ESCROW_PAYMENT_ENABLED", False)
        _patch_vendors(monkeypatch, _tiered_vendor())
        cart_runtime = _cart_runtime()
        interpreter = make_input_interpreter("BUYER")

        # ── Tour 1 : quantité globale donnée AVANT tout menu ──────────────
        state = _open(
            cart_runtime, "je veux 30 L de lait",
            {"product": "lait", "quantity": 30, "unit": "LITRE"},
        )
        state = apply_patch(state, run(cart_management(state, cart_runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        state = _turn_boundary(state, cart_runtime)

        # ── Tour 2 : "2" -> résout le palier 10L, purge la quantité héritée ─
        state = _say(state, cart_runtime, interpreter, "2")
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert state["vendor_selection_context"]["resolved_tier_id"] == "t10"
        state = _turn_boundary(state, cart_runtime)

        # ── Tour 3 : "3" -> ajoute réellement au panier ────────────────────
        state = _say(state, cart_runtime, interpreter, "3")
        assert state["status"] == "COMPLETED"
        cart_line = state["active_cart"][-1]
        assert cart_line["tier_id"] == "t10"
        assert cart_line["quantity"] == 3  # package_count
        assert cart_line["base_unit_quantity"] == 30.0  # effective_quantity
        assert cart_line["price"] == 900.0  # prix du paquet
        assert cart_line["line_total"] == 2700.0
        state = _turn_boundary(state, cart_runtime)

        router = DomainRouter.build()
        buyer_phone = state["user_phone"]

        # ── Tour 4 : "confirmer" depuis le panier -> checkout RÉEL ────────
        checkout_state = dict(state)
        checkout_state["current_goal"] = "BUYER_PREORDER_CONFIRM"
        checkout_state["transaction_payload"] = {"resolved_id": "PREORDER_CONFIRM"}
        route = router.decide(checkout_state)
        assert isinstance(route, str) and route

        # Le serveur (canned ici) résout le palier de façon AUTORITATIVE —
        # les valeurs renvoyées doivent être EXACTEMENT celles déjà
        # calculées côté panier (`cart_line` ci-dessus), preuve que la
        # plomberie (tier_id transmis dans `items_payload`, voir
        # `flows/buyer/preorder.py::create_preorder`) ne perd rien en route.
        checkout_runtime = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success",
                    "preorder_id": "order-full-journey-1",
                    "total_amount": cart_line["line_total"],
                    "currency": "XOF",
                    "items": [
                        {
                            "product_id": cart_line["product_id"],
                            "name": cart_line["name"],
                            "quantity": cart_line["quantity"],
                            "unit": cart_line["unit"],
                            "packaging": cart_line["packaging"],
                            "tier_quantity": cart_line["tier_quantity"],
                            "base_unit_quantity": cart_line["base_unit_quantity"],
                            "tier_id": cart_line["tier_id"],
                            "price": cart_line["price"],
                            "line_total": cart_line["line_total"],
                            "producer_id": cart_line.get("producer_id"),
                        }
                    ],
                    "unresolved_items": [],
                }
            }
        )
        checkout_patch = run(create_preorder(checkout_state, checkout_runtime))
        assert "create_preorder_draft" in checkout_runtime.call_names

        # `create_preorder_draft` doit avoir reçu le `tier_id` — sans quoi
        # tout le correctif serveur (2026-09-04) ne serait jamais atteint.
        sent_kwargs = dict(checkout_runtime.calls[0][1])
        assert sent_kwargs["cart_items"][0]["tier_id"] == "t10"

        draft_v1 = checkout_patch.get("preorder_draft")
        assert draft_v1 is not None, f"draft absent: {checkout_patch}"
        assert draft_v1["status"] == PreorderDraftStatus.DRAFT.value
        assert draft_v1["total_amount"] == 2700.0
        assert draft_v1["items"][0]["tier_id"] == "t10"
        assert draft_v1["items"][0]["base_unit_quantity"] == 30.0
        assert draft_v1["items"][0]["price"] == 900.0
        # Aucun point de livraison connu -> GPS demandé, JAMAIS d'exécution.
        assert checkout_patch["status"] == "WAITING_INPUT"

        # ── Tour 5 : position GPS partagée -> exécution RÉELLE ─────────────
        gps_state = dict(checkout_state)
        gps_state.update(checkout_patch)
        gps_state["location_shared"] = True
        gps_state["location_outcome"] = "NEW_LOCATION_ACCEPTED"
        gps_state["location_lat"] = 12.3714
        gps_state["location_lon"] = -1.5197

        confirm_runtime = RecordingRuntime(
            responses={
                "confirm_preorder_draft": {
                    "status": "success",
                    "order_id": draft_v1["order_id"],
                    "total_amount": 2700.0,
                    "currency": "XOF",
                }
            }
        )
        final_patch = run(create_preorder(gps_state, confirm_runtime))
        assert "confirm_preorder_draft" in confirm_runtime.call_names

        final_draft = final_patch.get("preorder_draft")
        assert final_draft is not None
        assert final_draft["status"] == PreorderDraftStatus.EXECUTED.value
        assert final_draft["items"][0]["tier_id"] == "t10"
        assert final_draft["items"][0]["base_unit_quantity"] == 30.0
        assert final_draft["total_amount"] == 2700.0

        persisted = run(preorder_draft_store.load(draft_v1["draft_id"]))
        assert persisted is not None
        assert persisted.status == PreorderDraftStatus.EXECUTED
        assert persisted.total_amount == 2700.0

        # Même valeur PARTOUT : panier, snapshot checkout, draft confirmé.
        for value_set in (cart_line, draft_v1["items"][0], final_draft["items"][0]):
            assert value_set["tier_id"] == "t10"
            assert value_set["base_unit_quantity"] == 30.0
            assert value_set["price"] == 900.0
            assert value_set["line_total"] == 2700.0
