"""B6 (2026-10-02) — même produit répété pendant un tunnel Buyer actif : le contexte est préservé.

Incident prod : « lait » -> menu 7 producteurs -> « 2 » (Gilbert-prod, « Quelle quantité ? ») ->
« je veux acheter du lait » relançait une recherche catalogue et ré-affichait les 7 producteurs
(NEW_TASK BUYER_REQUEST -> INTERRUPTION -> goal_planner purge le contexte vendeur).

Priorité pendant un tunnel actif : produit DIFFÉRENT (switch, B4/B5) > MÊME produit (B6) > réponse
de slot valide > texte ambigu (chaîne cognitive normale).

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés (harnais de B4).
"""
from __future__ import annotations

import pytest

from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _HOSTILE,
    _POULETS,
    _Conv,
    _offer,
    _pending,
)

_DEVIATION = {"disposition": "DEVIATION", "confidence": 0.9}
_LAIT7 = [_offer("lait", "LITRE", f"Producteur{i}", f"L{i}") for i in range(7)]
_SAME = "je veux acheter du lait"


def _chosen(st):
    return ((st.get("vendor_selection_context") or {}).get("chosen_vendor") or {}).get("vendor_name")


def _lait_cart(st):
    return [(x["name"], x["quantity"], x["unit"], x["vendor_name"]) for x in st["active_cart"]]


def _vendor_chosen(structured=_DEVIATION, offers=None):
    c = _Conv(offers or _LAIT7, structured)
    c.say("je veux acheter du lait")
    st = c.say("2")
    assert "ENTER_QUANTITY" in _pending(st) and _chosen(st) == "Producteur1"
    return c, st


class TestSameProductDuringQuantity:
    def test_prod_replay_vendor_pricing_pending_preserved_no_search(self):
        c, before = _vendor_chosen()
        c.rt.tool_log.clear()
        st = c.say(_SAME)
        assert c.rt.of("search_products") == [], "recherche catalogue relancée"
        assert _chosen(st) == "Producteur1"
        assert (st["vendor_selection_context"]["chosen_vendor"]["price"]) == 3500.0
        assert "ENTER_QUANTITY" in _pending(st)
        assert "Quelle quantité" in st["final_response"] and "Producteur1" in st["final_response"]
        assert "Producteurs disponibles" not in st["final_response"]
        assert int(st.get("retry_count") or 0) == 0
        assert st["active_cart"] == [] and c.rt.of("validate_stock_availability_atomic") == []

    @pytest.mark.parametrize("variant", ["invalid_json", "unknown", "hallucinated_qty", "exception"])
    def test_hostile_parser_cannot_break_the_continuation(self, variant):
        c, _ = _vendor_chosen(_HOSTILE[variant])
        c.rt.tool_log.clear()
        st = c.say(_SAME)
        assert c.rt.of("search_products") == [] and _chosen(st) == "Producteur1"
        assert "ENTER_QUANTITY" in _pending(st) and int(st.get("retry_count") or 0) == 0

    def test_idempotent_three_repeats(self):
        c, _ = _vendor_chosen()
        c.rt.tool_log.clear()
        for _ in range(3):
            st = c.say(_SAME)
        assert c.rt.of("search_products") == []
        assert _chosen(st) == "Producteur1" and "ENTER_QUANTITY" in _pending(st)
        assert int(st.get("retry_count") or 0) == 0 and st["active_cart"] == []

    @pytest.mark.parametrize("text", ["du lait", "encore du lait", "lait", "je veux du lait"])
    def test_bare_mentions_keep_the_context(self, text):
        c, _ = _vendor_chosen()
        c.rt.tool_log.clear()
        st = c.say(text)
        assert c.rt.of("search_products") == [] and _chosen(st) == "Producteur1"
        assert "ENTER_QUANTITY" in _pending(st)

    @pytest.mark.parametrize("text,qty", [("je veux acheter 5 L de lait", 5.0), ("je veux 10 litres de lait", 10.0)])
    def test_same_product_with_quantity_goes_to_the_chosen_vendor(self, text, qty):
        c, _ = _vendor_chosen()
        c.rt.tool_log.clear()
        st = c.say(text)
        assert c.rt.of("search_products") == []
        assert _lait_cart(st) == [("lait", qty, "LITRE", "Producteur1")]
        stock = c.rt.of("validate_stock_availability_atomic")
        assert [(k["product_id"], k["quantity"]) for k in stock] == [("L1", qty)]

    def test_pure_quantity_still_works(self):
        c, _ = _vendor_chosen({"disposition": "ACTION", "action": "SET_QUANTITY", "quantity": 2,
                               "unit": None, "confidence": 0.95})
        st = c.say("2")
        assert _lait_cart(st) == [("lait", 2.0, "LITRE", "Producteur1")]


class TestSameProductDuringProducerMenu:
    def test_menu_is_kept_without_new_search_or_retry(self):
        c = _Conv(_LAIT7, _DEVIATION)
        st = c.say("je veux acheter du lait")
        assert len(st["expected_candidates"]) == 7
        c.rt.tool_log.clear()
        st = c.say(_SAME)
        assert c.rt.of("search_products") == []
        assert len(st["expected_candidates"]) == 7
        assert st["final_response"].count("*1.*") == 1, "menu dupliqué"
        assert int(st.get("retry_count") or 0) == 0 and st["active_cart"] == []
        st = c.say("2")  # le menu d'origine reste sélectionnable
        assert _chosen(st) == "Producteur1"

    def test_quantity_given_during_the_menu_is_kept_not_lost_or_applied_blindly(self):
        c = _Conv(_LAIT7, _DEVIATION)
        c.say("je veux acheter du lait")
        c.rt.tool_log.clear()
        st = c.say("je veux acheter 5 L de lait")
        assert c.rt.of("search_products") == [] and len(st["expected_candidates"]) == 7
        assert st["active_cart"] == []  # aucune mutation de panier avant le choix du producteur
        st = c.say("2")
        assert _chosen(st) == "Producteur1" and c.rt.of("search_products") == []
        assert st["active_cart"] == []  # contrat existant : la quantité est redemandée après le choix


class TestSameProductDuringTierMenu:
    @staticmethod
    def _tiered():
        o = _offer("lait", "LITRE", "Ferme0", "L0")
        o["pricing_tiers"] = [
            {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 500.0, "packaging": "bidon",
             "base_unit_quantity": 5.0, "min_order_quantity": 1},
            {"tier_id": "t10", "quantity": 10.0, "unit": "L", "price": 900.0, "packaging": "bidon",
             "base_unit_quantity": 10.0, "min_order_quantity": 1},
        ]
        return o

    def test_vendor_and_tiers_preserved(self):
        c = _Conv([self._tiered()], _DEVIATION)
        st = c.say("je veux acheter du lait")
        assert "conditionnements" in st["final_response"]
        c.rt.tool_log.clear()
        st = c.say(_SAME)
        assert c.rt.of("search_products") == []
        assert "conditionnements" in st["final_response"]
        assert st["tier_selection_context"].get("tiers") and _chosen(st) == "Ferme0"
        assert int(st.get("retry_count") or 0) == 0 and st["active_cart"] == []


class TestPriorityAndBoundaries:
    def test_different_product_still_switches(self):
        c, _ = _vendor_chosen(offers=_LAIT7 + [_POULETS])
        c.rt.tool_log.clear()
        st = c.say("je veux acheter des poulets")
        assert any(k.get("product") == "poulets" for k in c.rt.of("search_products"))
        assert "poulets" in st["final_response"] and _chosen(st) != "Producteur1"

    def test_completed_flow_allows_a_new_purchase_of_the_same_product(self):
        c, _ = _vendor_chosen()
        st = c.say("je veux acheter 2 L de lait")
        assert _lait_cart(st) == [("lait", 2.0, "LITRE", "Producteur1")]
        assert "ENTER_QUANTITY" not in _pending(st)
        c.rt.tool_log.clear()
        st = c.say(_SAME)
        assert any(k.get("product") == "lait" for k in c.rt.of("search_products")), "nouvel achat bloqué"
        assert len(_lait_cart(st)) == 1  # pas de duplication de ligne

    @pytest.mark.parametrize("text", ["annuler", "je ne sais pas"])
    def test_cancel_and_dont_know_are_not_continuations(self, text):
        c, _ = _vendor_chosen({"disposition": "REJECT" if text == "annuler" else "UNKNOWN", "confidence": 0.9})
        c.rt.tool_log.clear()
        c.llm.families.clear()
        c.say(text)
        assert c.rt.of("search_products") == []
        assert "continuation" not in str(c.llm.families)


class TestDetectorUnit:
    @staticmethod
    def _state(pending="ENTER_QUANTITY", product="lait", goal="BUYER_ADD_TO_CART"):
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )
        from tests.conftest import make_state

        return make_state(
            current_goal=goal,
            transaction_payload={"product": product} if product else {},
            vendor_selection_context={"product": "lait", "vendors": [], "chosen_vendor": {"name": "lait", "vendor_name": "G"}},
            **set_pending_interaction(InteractionKind[pending], field_name="quantity"),
        )

    @pytest.mark.parametrize("text", ["je veux acheter du lait", "je veux du lait", "du lait", "encore du lait",
                                      "lait", "je cherche du lait", "je veux plutôt du lait"])
    def test_positive(self, text):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_product_switch,
            detect_buyer_same_product_continuation,
        )

        st = self._state()
        assert detect_buyer_same_product_continuation(st, text) is not None, text
        assert detect_buyer_product_switch(st, text) is None, "same-product ne doit jamais être un switch"

    @pytest.mark.parametrize("text", ["2", "5 kg", "annuler", "oui", "non", "je ne sais pas", "un peu",
                                      "je veux acheter des poulets", "je veux acheter du pain"])
    def test_negative(self, text):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_same_product_continuation,
        )

        assert detect_buyer_same_product_continuation(self._state(), text) is None, text

    def test_quantity_extracted_only_for_a_base_unit_slot(self):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_same_product_continuation,
        )

        got = detect_buyer_same_product_continuation(self._state(), "je veux 10 litres de lait")
        assert got is not None and (got.quantity, got.unit) == (10.0, "LITRE")
        # (menu PRODUCTEUR + quantité : couvert en e2e, format réel du contexte vendeur)
        # menu de PALIERS : « 2 bidons » est un nombre de paquets -> parser du tunnel
        tier_menu = self._state(pending="SELECTION_MENU")
        tier_menu["tier_selection_context"] = {"tiers": [{"tier_id": "t5", "quantity": 5.0, "unit": "L"}]}
        assert detect_buyer_same_product_continuation(tier_menu, "je veux 2 bidons de lait") is None

    def test_needs_an_active_cart_tunnel(self):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_same_product_continuation,
        )
        from tests.conftest import make_state

        done = make_state(current_goal="BUYER_ADD_TO_CART", transaction_payload={"product": "lait"})
        assert detect_buyer_same_product_continuation(done, _SAME) is None
        assert detect_buyer_same_product_continuation(self._state(goal="SALES_PUBLISH_PRODUCT"), _SAME) is None
