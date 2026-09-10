"""`flows/buyer/cart.py::cart_management` — le chemin EXACT de l'incident réel
(2026-08-14) : produit ET quantité fournis dans le même message ("la dizaine
d'œufs à 500 FCFA") court-circuitaient directement vers
`add_to_cart_with_ref` → `reserve_future_offer`, sans jamais vérifier que le
produit retrouvé par la recherche floue ("Bœuf", 486 000 FCFA/tête) était
réellement ce que l'acheteur avait demandé ("oeufs").

Première correction : demander confirmation ("le plus proche est Bœuf,
c'est bien ça ?"). Retour utilisateur : le catalogue ne contient carrément
PAS d'œufs ni de laitue — suggérer "Bœuf" pour une recherche d'œufs est
trompeur, pas utile à confirmer. Un match qui n'est QUE trigram (pas de
sous-texte réel entre le terme cherché et le nom trouvé) est maintenant
écarté purement et simplement, et traité exactement comme "aucun résultat"
(propose un appel d'offres) — voir [[buyer-search-fuzzy-match-safety-2026-08]]."""
from __future__ import annotations

from tests.conftest import StubRuntime, make_state, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


class TestLowConfidenceMatchIsTreatedAsNotFound:
    def test_does_not_reserve_and_falls_back_to_the_not_found_flow(self):
        from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
        state = make_state(
            user_phone="+2260",
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "oeufs", "quantity": 10},
        )
        runtime = rt({"search_products": {
            "status": "success",
            "results": [{
                "id": "offer1", "vendor_name": "jojo", "unit": "TETE",
                "price": 486000, "name": "Bœuf (🌐 National)", "source_type": "FUTURE",
            }],
        }})

        result = run(cart_management(state, runtime))

        assert "reserve_future_offer" not in runtime.calls
        assert "Bœuf" not in result["final_response"]
        assert "disponible" in result["final_response"].lower() or "n'est pas" in result["final_response"]

    def test_a_confident_single_match_still_uses_the_normal_flow(self):
        """Non-régression : seuls les matches trigram-only sont écartés — un
        match avec une vraie relation textuelle (substring) continue de
        fonctionner normalement."""
        from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
        state = make_state(
            user_phone="+2260",
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "oeufs", "quantity": 10},
        )
        runtime = rt({
            "search_products": {
                "status": "success",
                "results": [{
                    "id": "offer1", "vendor_name": "jojo", "unit": "PLAQUETTE",
                    "price": 1500, "name": "Œufs (🌐 National)", "source_type": "DIRECT",
                }],
            },
            "add_to_cart": {"status": "success"},
        })

        result = run(cart_management(state, runtime))

        assert "reserve_future_offer" not in runtime.calls


class TestConfidentMatchIsUnaffected:
    def test_a_confident_match_with_quantity_still_checks_out_directly(self):
        """Non-régression : le chemin rapide existant (produit reconnu avec
        certitude + quantité déjà connue) doit continuer à fonctionner sans
        étape superflue."""
        from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
        state = make_state(
            user_phone="+2260",
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "tomate", "quantity": 5},
        )
        runtime = rt({
            "search_products": {
                "status": "success",
                "results": [{
                    "id": "p1", "vendor_name": "Awa", "unit": "KG",
                    "price": 250, "name": "Tomate (🌐 National)", "source_type": "DIRECT",
                }],
            },
        })

        result = run(cart_management(state, runtime))

        assert result.get("expected_input") != "CONFIRMATION"
