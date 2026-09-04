"""Séparation stricte `requested_quantity` ≠ `selected_tier` ≠ `package_count`.

Audit 2026-09-01 (directive « MENU-FIRST », suite de
`test_tier_menu_first_golden_path.py`). Les tests existants prouvaient déjà le
chemin nominal *quand l'acheteur répond par un chiffre nu*. Ce fichier verrouille
les trois trous reproduits ce jour-là sur le VRAI enchaînement de nœuds
(interpreter → memory_update → validator → cart_management → cleanup) :

1. **Héritage toxique à l'étape nombre-de-paquets.** Sur "Combien de bidons de
   10 L ?", la réponse "30 litres" était acceptée comme 30 PAQUETS → 300 L
   facturés 27 000 FCFA. Deux causes cumulées, toutes deux corrigées :
   le fast-path fabriquait une unité depuis le payload FUSIONNÉ (l'unité de la
   demande initiale, donnée avant même l'affichage des paliers), et le garde
   d'unité du domaine ne rejetait que les familles INCOMPATIBLES — or `L` et
   `litre` normalisent vers la même unité que le palier, donc il passait.
2. **Aucun changement d'avis possible.** `tier_selection_context` était détruit
   dès le palier résolu : plus aucune liste à résoudre, donc "finalement le
   bidon de 5 L" retombait en quantité (5 paquets du MAUVAIS palier).
3. **Refus de stock incohérent.** Le message comparait la disponibilité (unité
   de base) au nombre de PAQUETS : « seulement 500 LITRE sur 60 LITRE
   demandé(s) » pour 60 bidons de 10 L.

Couvre les cas A à H du cahier des charges.
"""
from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.flows.buyer.cart import cart_management
from agriconnect.graphs.agents.market_coach.interpreter.routing import (
    make_input_interpreter,
)
from agriconnect.graphs.agents.market_coach.nodes.cleaner import state_cleaner_node
from agriconnect.graphs.agents.market_coach.nodes.cleanup import post_response_cleanup
from agriconnect.graphs.agents.market_coach.nodes.memory import memory_update
from agriconnect.graphs.agents.market_coach.nodes.validation import validator
from tests.conftest import ScriptedLLM, StubRuntime, run
from tests.integration.test_tier_selection_full_node_chain import apply_patch
from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)

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


def _flat_vendor() -> Dict[str, Any]:
    """Produit HISTORIQUE : aucun palier, tarif unique (cas D)."""
    return {
        "product_id": "P-TOM", "name": "tomates", "price": 500.0, "unit": "LITRE",
        "vendor_name": "jojo", "producer_id": "PR-JOJO", "source_type": "DIRECT",
        "is_auction": False, "pricing_tiers": None,
    }


def _runtime(available: float = 500.0, ok: bool = True) -> StubRuntime:
    stock: Dict[str, Any] = (
        {"status": "success", "available_quantity": available, "unit": "LITRE",
         "unit_price": 500.0}
        if ok
        else {"status": "error", "reason": "insufficient_stock",
              "available_quantity": available, "unit": "LITRE"}
    )
    return StubRuntime(responses={"validate_stock_availability_atomic": stock})


def _patch_vendors(monkeypatch, vendor: Dict[str, Any]) -> None:
    import agriconnect.graphs.agents.market_coach.services.domain.cart_service as m

    async def _fake(self, phone, product_name):
        return [vendor], False

    monkeypatch.setattr(m.CartDomainService, "resolve_product_vendors", _fake)


def _turn_boundary(state: Dict[str, Any], runtime: StubRuntime) -> Dict[str, Any]:
    state = apply_patch(state, run(state_cleaner_node(state, runtime)))
    return apply_patch(state, run(post_response_cleanup(state, runtime)))


def _say(state, runtime, interpreter, text: str) -> Dict[str, Any]:
    """Un tour utilisateur complet à travers la vraie chaîne de nœuds."""
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
        "user_phone": "+22601479800", "transaction_payload": payload,
        "working_memory": {}, "active_cart": [],
    }


class TestNoToxicQuantityInheritance:
    """Cas B + C : « je veux 30 L de lait » ne devient JAMAIS 30 paquets."""

    def test_upfront_quantity_never_becomes_a_pack_count(self, monkeypatch):
        _patch_vendors(monkeypatch, _tiered_vendor())
        runtime = _runtime()
        interpreter = make_input_interpreter("BUYER")

        # --- Tour 1 : la quantité globale est donnée AVANT tout menu --------
        state = _open(
            runtime, "je veux 30 L de lait",
            {"product": "lait", "quantity": 30, "unit": "LITRE"},
        )
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "SELECTION"
        assert "conditionnements" in state["final_response"]
        state = _turn_boundary(state, runtime)

        # --- Tour 2 : "2" → palier 10 L, la quantité héritée est PURGÉE ----
        state = _say(state, runtime, interpreter, "2")
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert state["vendor_selection_context"]["resolved_tier_id"] == "t10"
        assert not state["active_cart"], "rien ne doit entrer au panier ici"
        assert (state.get("transaction_payload") or {}).get("quantity") in (
            None, "", 0
        ), "package_count doit être None après la seule sélection du palier"
        assert "30" not in state["final_response"], (
            "le 30 pré-palier ne doit apparaître nulle part comme un compte"
        )
        state = _turn_boundary(state, runtime)

        # --- Tour 3 : "3" → 3 × 900 = 2700 FCFA, 30 L au total -------------
        state = _say(state, runtime, interpreter, "3")
        assert state["status"] == "COMPLETED"
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10"
        assert line["quantity"] == 3, "package_count"
        assert line["tier_quantity"] == 10.0, "quantity_per_package"
        assert line["base_unit_quantity"] == 30.0, "total_quantity"
        assert line["price"] == 900.0, "unit_price = prix du PAQUET"
        assert line["line_total"] == 2700.0, "total_price = 3 × 900"

    def test_a_bare_pack_count_never_inherits_the_initial_unit(self, monkeypatch):
        """Cause racine n°1 (audit initial) : le fast-path complétait un "3" nu
        avec l'unité du payload FUSIONNÉ ("LITRE", héritée de "30 L de lait" au
        tour 1). Depuis le contrat d'action structurée (2026-09-01,
        domain/selection_actions.py), un "3" nu sous `SET_PACKAGE_COUNT`
        produit directement `action_package_count=3` — un champ dédié qui n'a
        structurellement pas de case "unit" où une valeur pourrait fuiter."""
        _patch_vendors(monkeypatch, _tiered_vendor())
        runtime = _runtime()
        interpreter = make_input_interpreter("BUYER")
        state = _open(
            runtime, "je veux 30 L de lait",
            {"product": "lait", "quantity": 30, "unit": "LITRE"},
        )
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = _turn_boundary(state, runtime)
        state = _say(state, runtime, interpreter, "2")
        state = _turn_boundary(state, runtime)

        state["normalized_text"] = "3"
        state["user_query"] = "3"
        state = apply_patch(state, run(interpreter(state, runtime)))
        assert state["extracted_entities"].get("agent_action") == "SET_PACKAGE_COUNT"
        assert state["extracted_entities"].get("action_package_count") == 3.0
        assert state["extracted_entities"].get("quantity") is None
        assert state["extracted_entities"].get("unit") is None, (
            "un nombre de paquets est SANS DIMENSION — aucune unité héritée"
        )


class TestPackCountSlotRejectsATotalQuantity:
    """Cause racine n°2 : « 30 litres » accepté comme 30 paquets (27 000 FCFA)."""

    def test_answering_the_pack_count_with_a_total_quantity_is_refused(
        self, monkeypatch
    ):
        _patch_vendors(monkeypatch, _tiered_vendor())
        runtime = _runtime()
        interpreter = make_input_interpreter("BUYER")
        state = _open(runtime, "je veux du lait", {"product": "lait"})
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = _turn_boundary(state, runtime)
        state = _say(state, runtime, interpreter, "2")
        state = _turn_boundary(state, runtime)

        state = _say(state, runtime, interpreter, "30 litres")
        assert state["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert not state["active_cart"], (
            "30 litres NE DOIT PAS devenir 30 bidons (300 L / 27 000 FCFA)"
        )
        body = state["final_response"].lower()
        assert "27000" not in body and "300" not in body
        assert "combien de" in body, "la question doit être reposée explicitement"

    def test_no_automatic_tetris_from_a_total_quantity(self, monkeypatch):
        """Règle métier explicite : 30 L ne doit pas non plus être résolu
        automatiquement en « 3 bidons de 10 L ». On redemande, point."""
        _patch_vendors(monkeypatch, _tiered_vendor())
        runtime = _runtime()
        interpreter = make_input_interpreter("BUYER")
        state = _open(runtime, "je veux du lait", {"product": "lait"})
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = _turn_boundary(state, runtime)
        state = _say(state, runtime, interpreter, "2")
        state = _turn_boundary(state, runtime)
        state = _say(state, runtime, interpreter, "30 litres")
        assert not state["active_cart"]
        assert state["vendor_selection_context"]["resolved_tier_id"] == "t10", (
            "le palier reste celui choisi — rien n'est recalculé en douce"
        )


class TestTierReselection:
    """Cas F : « 2 » puis « finalement le bidon de 5 L »."""

    def test_an_explicit_tier_change_is_applied_and_acknowledged(self, monkeypatch):
        _patch_vendors(monkeypatch, _tiered_vendor())
        runtime = _runtime()
        interpreter = make_input_interpreter("BUYER")
        state = _open(runtime, "je veux du lait", {"product": "lait"})
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = _turn_boundary(state, runtime)
        state = _say(state, runtime, interpreter, "2")
        assert state["vendor_selection_context"]["resolved_tier_id"] == "t10"
        state = _turn_boundary(state, runtime)

        # Le LLM (seul à recevoir la liste des paliers) désigne l'autre palier
        # via le contrat d'action structurée (2026-09-01).
        runtime.llm = ScriptedLLM({
            "interpreted_event": "SELECTION",
            "detected_intent": "BUYER_ADD_TO_CART",
            "confidence": 0.9,
            "extracted_entities": {
                "agent_action": "SELECT_PRICING_TIER",
                "action_pricing_tier_id": "t5",
            },
        })
        state = _say(state, runtime, interpreter, "finalement le bidon de 5 L")

        assert state["vendor_selection_context"]["resolved_tier_id"] == "t5"
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        assert not state["active_cart"], (
            "le 5 de « 5 L » désigne le CONDITIONNEMENT, jamais 5 paquets"
        )
        assert "5.0 L" in state["final_response"]
        state = _turn_boundary(state, runtime)

        # Le palier est fixé — la réponse au nombre de paquets qui suit passe
        # par le chemin déterministe (pas d'appel LLM scripté pour "3").
        runtime.llm = None
        state = _say(state, runtime, interpreter, "3")
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t5"
        assert line["line_total"] == 1500.0, "3 × 500 FCFA"
        assert line["base_unit_quantity"] == 15.0

    def test_a_bare_digit_after_the_tier_is_a_pack_count_not_a_menu_index(
        self, monkeypatch
    ):
        """Le même texte « 1 » signifie « palier n°1 » sous SELECTION et
        « 1 paquet » sous nombre-de-paquets. Sans état, impossible à trancher."""
        _patch_vendors(monkeypatch, _tiered_vendor())
        runtime = _runtime()
        interpreter = make_input_interpreter("BUYER")
        state = _open(runtime, "je veux du lait", {"product": "lait"})
        state = apply_patch(state, run(cart_management(state, runtime)))
        state = _turn_boundary(state, runtime)
        state = _say(state, runtime, interpreter, "2")  # → palier 10 L
        state = _turn_boundary(state, runtime)

        state = _say(state, runtime, interpreter, "1")  # → 1 PAQUET, pas t5
        line = state["active_cart"][-1]
        assert line["tier_id"] == "t10", "un chiffre nu ne re-sélectionne pas"
        assert line["quantity"] == 1
        assert line["line_total"] == 900.0


class TestStockValidation:
    """Cas G : 60 paquets × 10 L = 600 L > 500 L disponibles."""

    def test_pack_count_exceeding_stock_is_refused_with_coherent_numbers(self):
        from agriconnect.graphs.agents.market_coach.services.domain.cart_service import (
            CartDomainService,
        )

        svc = CartDomainService(_runtime(available=500.0, ok=False))
        result = run(
            svc.add_to_cart_with_ref(
                "+22601479800", "lait", 60, _tiered_vendor(), [], {},
                buyer_unit=None, tier_id="t10",
            )
        )
        assert result["status"] == "WAITING_INPUT"
        assert "active_cart" not in result, "commande refusée : panier intact"
        body = result["final_response"]
        assert "600" in body, "la demande doit s'afficher en unité de BASE (600 L)"
        assert "60 bidon" in body, "le nombre de paquets reste explicité"
        # L'appel d'offres proposé en repli doit porter la quantité réelle.
        assert result["transaction_payload"]["quantity"] == 600.0
        assert result["transaction_payload"]["unit"] == "LITRE"


class TestHistoricalFlatProductUnchanged:
    """Cas D : produit sans `pricing_tiers` — comportement historique intact."""

    def test_flat_product_multiplies_price_by_the_raw_quantity(self, monkeypatch):
        _patch_vendors(monkeypatch, _flat_vendor())
        runtime = _runtime()
        state = _open(
            runtime, "je veux 30 L de tomates",
            {"product": "tomates", "quantity": 30, "unit": "LITRE"},
        )
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert state["status"] == "COMPLETED"
        line = state["active_cart"][-1]
        assert line["quantity"] == 30.0
        assert line["unit"] == "LITRE"
        assert line["line_total"] == 15000.0  # 30 × 500
        assert "tier_id" not in line
        assert "base_unit_quantity" not in line

    def test_flat_product_still_inherits_its_unit_on_a_bare_number(self, monkeypatch):
        """Garde anti-régression du correctif fast-path : la suppression de
        l'héritage d'unité ne vaut QUE pendant l'attente d'un nombre de
        paquets — un produit à tarif unique doit continuer d'hériter."""
        _patch_vendors(monkeypatch, _flat_vendor())
        runtime = _runtime()
        interpreter = make_input_interpreter("BUYER")
        state = _open(runtime, "je veux des tomates", {"product": "tomates"})
        state = apply_patch(state, run(cart_management(state, runtime)))
        assert to_tunnel_category(get_pending_interaction(state)) == "QUANTITY"
        state = _turn_boundary(state, runtime)
        state["transaction_payload"] = dict(state.get("transaction_payload") or {})
        state["transaction_payload"]["unit"] = "LITRE"

        state["normalized_text"] = "30"
        state["user_query"] = "30"
        state = apply_patch(state, run(interpreter(state, runtime)))
        assert state["extracted_entities"].get("quantity") == 30.0
        assert state["extracted_entities"].get("unit") == "LITRE"


class TestNoMenuLoop:
    """Cas H : le menu s'affiche UNE fois, la sélection aboutit, le prix tombe."""

    def test_full_sequence_shows_the_menu_exactly_once(self, monkeypatch):
        _patch_vendors(monkeypatch, _tiered_vendor())
        runtime = _runtime()
        interpreter = make_input_interpreter("BUYER")
        seen = []

        state = _open(runtime, "je veux du lait", {"product": "lait"})
        state = apply_patch(state, run(cart_management(state, runtime)))
        seen.append(state["final_response"])
        state = _turn_boundary(state, runtime)

        state = _say(state, runtime, interpreter, "2")
        seen.append(state["final_response"])
        state = _turn_boundary(state, runtime)

        state = _say(state, runtime, interpreter, "3")
        seen.append(state["final_response"])

        menus = [r for r in seen if "conditionnements" in (r or "")]
        assert len(menus) == 1, f"menu affiché {len(menus)} fois : {seen}"
        assert "Combien de" in seen[1]
        assert state["active_cart"][-1]["line_total"] == 2700.0
