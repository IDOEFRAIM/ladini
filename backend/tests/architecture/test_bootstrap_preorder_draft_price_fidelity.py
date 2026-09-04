"""`bootstrap_preorder_draft` — le récapitulatif affiché à l'acheteur doit
refléter EXACTEMENT ce que `create_preorder_draft` a écrit en base, jamais
le panier figé au moment de l'ajout (2026-09-04, audit CART→CHECKOUT).

## Le gap réel fermé par ce test

`create_preorder_draft` (services/database/buyer.py) relit
`Product.price`/`Product.pricing_tiers` FRAIS en base et peut donc calculer
un total DIFFÉRENT du panier local (prix catalogue modifié entre l'ajout et
le checkout — mandat "price revalidation"). AVANT ce correctif,
`bootstrap_preorder_draft` ignorait totalement cette réponse et construisait
`PreorderDraft.items`/`total_amount` à partir du panier LOCAL (`meta`/
`items_payload`, potentiellement périmés) — exactement la classe de bug
"affichage ≠ exécution" que toute cette architecture existe pour éliminer :
l'acheteur aurait confirmé un total, et la commande réellement créée en
aurait porté un autre."""
from __future__ import annotations

from tests.conftest import run
from tests.architecture.test_preorder_draft_persistence import _install_fake_db
from tests.evals.runners.harness import RecordingRuntime

from agriconnect.graphs.agents.market_coach.flows.buyer.preorder_confirmation import (
    bootstrap_preorder_draft,
)


class TestBootstrapUsesServerAuthoritativePricingNotStaleCartSnapshot:
    def test_a_price_changed_between_add_to_cart_and_checkout_is_reflected_in_the_draft(
        self, monkeypatch
    ):
        _install_fake_db(monkeypatch)

        # Panier LOCAL figé au moment de l'ajout — prix=250 (périmé : le
        # producteur a depuis changé son tarif catalogue à 300).
        stale_items_payload = [
            {
                "product_id": "prod-1",
                "name": "Tomates",
                "quantity": 50.0,
                "unit": "KG",
                "price": 250.0,
                "producer_id": "farm-1",
            }
        ]
        stale_meta = {"total_amount": 12500.0, "currency": "XOF"}  # 50*250, PÉRIMÉ

        # Réponse SERVEUR de `create_preorder_draft` — le prix RÉEL en base
        # (300, pas 250) au moment du checkout.
        runtime = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success",
                    "preorder_id": "order-1",
                    "total_amount": 15000.0,  # 50*300, l'AUTORITATIF
                    "currency": "XOF",
                    "items": [
                        {
                            "product_id": "prod-1",
                            "name": "Tomates",
                            "quantity": 50.0,
                            "unit": "KG",
                            "price": 300.0,
                            "line_total": 15000.0,
                            "producer_id": "farm-1",
                        }
                    ],
                }
            }
        )

        patch = run(
            bootstrap_preorder_draft(
                {"user_phone": "+22670000001"},
                runtime,
                items_payload=stale_items_payload,
                meta=stale_meta,
            )
        )

        draft = patch["preorder_draft"]
        # Le draft — donc `render_summary()`, donc ce que l'acheteur va
        # confirmer — porte le total et le prix RÉELS (300/15000), jamais
        # les valeurs périmées du panier local (250/12500).
        assert draft["total_amount"] == 15000.0
        assert draft["items"][0]["price"] == 300.0
        assert draft["items"][0]["line_total"] == 15000.0
        assert "12500" not in patch.get("final_response", "")
        assert "15000" in patch.get("final_response", "") or "15000.0" in str(draft["total_amount"])

    def test_degraded_server_response_without_items_falls_back_to_local_cart(self, monkeypatch):
        """Rétro-compatibilité — un serveur MCP qui ne renvoie pas encore
        `items`/`total_amount` (ancien déploiement non redéployé) ne doit
        jamais faire planter le tour : repli sûr sur le panier local plutôt
        qu'un crash, journalisé pour rester visible."""
        _install_fake_db(monkeypatch)
        items_payload = [
            {"product_id": "prod-1", "name": "Maïs", "quantity": 10.0, "unit": "KG", "price": 100.0}
        ]
        meta = {"total_amount": 1000.0, "currency": "XOF"}
        runtime = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success",
                    "preorder_id": "order-2",
                    # PAS de "items"/"total_amount" — ancien format.
                }
            }
        )
        patch = run(
            bootstrap_preorder_draft(
                {"user_phone": "+22670000002"}, runtime, items_payload=items_payload, meta=meta
            )
        )
        draft = patch["preorder_draft"]
        assert draft["total_amount"] == 1000.0
        assert draft["items"] == items_payload


class TestBootstrapSurfacesServerDroppedItemsNeverSilently:
    def test_an_item_the_server_dropped_is_explicitly_flagged_to_the_buyer(
        self, monkeypatch
    ):
        """(2026-09-04, clôture frontière CART→CHECKOUT) — `create_preorder_draft`
        peut écarter un article (palier périmé, ou désormais seuil minimum
        non atteint — voir `test_create_preorder_draft_minimum_order.py`)
        sans jamais faire échouer toute la précommande. AVANT ce correctif,
        rien n'informait l'acheteur que son panier de 2 articles n'en a
        produit qu'1 : le récapitulatif se serait tu sur la différence —
        exactement la divergence affichage≠exécution que cette architecture
        existe pour fermer."""
        _install_fake_db(monkeypatch)
        items_payload = [
            {"product_id": "prod-1", "name": "Maïs", "quantity": 200.0, "unit": "KG", "price": 100.0},
            {"product_id": "prod-2", "name": "Riz", "quantity": 5.0, "unit": "KG", "price": 100.0},
        ]
        meta = {"total_amount": 20500.0, "currency": "XOF"}
        runtime = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success",
                    "preorder_id": "order-3",
                    "total_amount": 20000.0,
                    "currency": "XOF",
                    "items": [
                        {
                            "product_id": "prod-1",
                            "name": "Maïs",
                            "quantity": 200.0,
                            "unit": "KG",
                            "price": 100.0,
                            "line_total": 20000.0,
                            "producer_id": "farm-1",
                        }
                    ],
                    "unresolved_items": ["prod-2"],
                }
            }
        )
        patch = run(
            bootstrap_preorder_draft(
                {"user_phone": "+22670000003"}, runtime, items_payload=items_payload, meta=meta
            )
        )
        assert "n'ont pas pu être inclus" in str(patch.get("final_response"))
        draft = patch["preorder_draft"]
        assert len(draft["items"]) == 1
        assert draft["total_amount"] == 20000.0

    def test_no_unresolved_items_leaves_the_response_untouched(self, monkeypatch):
        _install_fake_db(monkeypatch)
        items_payload = [
            {"product_id": "prod-1", "name": "Maïs", "quantity": 10.0, "unit": "KG", "price": 100.0}
        ]
        meta = {"total_amount": 1000.0, "currency": "XOF"}
        runtime = RecordingRuntime(
            responses={
                "create_preorder_draft": {
                    "status": "success",
                    "preorder_id": "order-4",
                    "total_amount": 1000.0,
                    "currency": "XOF",
                    "items": items_payload,
                    "unresolved_items": [],
                }
            }
        )
        patch = run(
            bootstrap_preorder_draft(
                {"user_phone": "+22670000004"}, runtime, items_payload=items_payload, meta=meta
            )
        )
        assert "n'ont pas pu être inclus" not in str(patch.get("final_response"))
