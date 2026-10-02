"""B5 (2026-10-02) — nouvelle demande d'achat pendant un menu de sélection producteur/palier.

Incident prod : « poulets » -> « je veux acheter du lait » (menu de 7 producteurs, pending
SELECTION_MENU) -> une nouvelle demande d'achat restait prisonnière de l'ancien menu (parser
STRUCTURED_ACTION consulté, menu ré-affiché, retry++). B4 ne couvrait que ENTER_QUANTITY.

Replays sur le VRAI graphe compilé ; seuls le LLM et le MCP sont doublés (harnais de B4).
"""
from __future__ import annotations

import pytest

from tests.integration.test_buyer_deterministic_product_switch_e2e import (
    _HOSTILE,
    _LAIT3,
    _POULETS,
    _TOMATES,
    _Conv,
    _live_products,
    _offer,
    _pending,
)

_LAIT7 = [_offer("lait", "LITRE", f"Producteur{i}", f"L{i}") for i in range(7)]
_BAD = ("On continue", "Max retries", "Veuillez choisir une option")


def _not_trapped(st, c, product):
    text = st.get("final_response") or ""
    assert not any(m in text for m in _BAD), text
    assert int(st.get("retry_count") or 0) == 0, "retry du menu précédent consommé"
    assert c.searched(product), c.llm.families
    assert "SELECTION_MENU" not in _pending(st) or product in text.lower()


@pytest.mark.parametrize("variant", ["invalid_json", "out_of_schema_action", "unknown", "hallucinated_qty", "exception"])
class TestSwitchDuringVendorMenu:
    @pytest.mark.parametrize("lait", [_LAIT3, _LAIT7], ids=["3_vendors", "7_vendors"])
    def test_new_purchase_leaves_the_producer_menu(self, variant, lait):
        c = _Conv([_POULETS] + lait + _TOMATES, _HOSTILE[variant])
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter du lait")
        assert len(st["expected_candidates"]) == len(lait)
        c.llm.families.clear()
        st = c.say("je veux acheter des tomates")
        _not_trapped(st, c, "tomates")
        assert "structured" not in c.llm.families and "active_slot" not in c.llm.families
        assert _live_products(st) == {"tomates"}, "menu/contexte lait recyclé"
        assert "Quelle quantité" in st["final_response"] and "tomates" in st["final_response"]
        assert st["active_cart"] == [] and c.rt.of("validate_stock_availability_atomic") == []

    def test_switch_with_quantity_goes_to_the_new_product(self, variant):
        c = _Conv([_POULETS] + _LAIT3 + _TOMATES, _HOSTILE[variant])
        c.say("je veux acheter des poulets")
        c.say("je veux acheter du lait")
        st = c.say("je veux acheter 5 kg de tomates")
        _not_trapped(st, c, "tomates")
        assert [(x["name"], x["quantity"], x["unit"]) for x in st["active_cart"]] == [("tomates", 5.0, "KG")]


class TestMultipleSwitches:
    def test_menu_to_menu_to_menu(self):
        c = _Conv([_POULETS] + _LAIT3 + _TOMATES, _HOSTILE["invalid_json"])
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter du lait")
        assert len(st["expected_candidates"]) == 3
        st = c.say("je veux acheter des tomates")
        assert _live_products(st) == {"tomates"}
        st = c.say("je veux acheter des poulets")
        assert _live_products(st) == {"poulets"}
        assert int(st.get("retry_count") or 0) == 0 and st["active_cart"] == []


class TestMenuRepliesAreNotSwitches:
    _QTY = {"disposition": "UNKNOWN", "confidence": 0.2}

    def test_digit_still_selects_the_producer(self):
        c = _Conv([_POULETS] + _LAIT3 + _TOMATES, self._QTY)
        c.say("je veux acheter des poulets")
        c.say("je veux acheter du lait")
        st = c.say("2")
        assert "Ferme1" in st["final_response"] and "Quelle quantité" in st["final_response"]
        assert not c.searched("tomates")

    @pytest.mark.parametrize("text", [
        "je veux celui de Ferme1", "je veux le premier", "je veux celui du dernier", "je veux le deuxième producteur",
        "annuler", "je ne sais pas", "oui", "non", "1",
    ])
    def test_option_references_do_not_switch(self, text):
        c = _Conv([_POULETS] + _LAIT3 + _TOMATES, self._QTY)
        c.say("je veux acheter des poulets")
        c.say("je veux acheter du lait")
        c.llm.families.clear()
        c.say(text)
        assert not c.searched("tomates") and not c.searched("poulets")
        assert "new_task" not in c.llm.families, (text, c.llm.families)


class TestDetectorOnMenus:
    @staticmethod
    def _menu_state():
        from tests.conftest import make_state

        return make_state(
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "lait"},
            vendor_selection_context={
                "product": "lait",
                "vendors": [{"name": "lait", "vendor_name": "Gilbert-prod", "producer_id": "p1",
                             "product_id": "a"}, {"name": "lait", "vendor_name": "OUEDRAOGO Jean",
                                                  "producer_id": "p2", "product_id": "b"}],
            },
            expected_candidates=["Gilbert-prod — 3500 FCFA/LITRE", "OUEDRAOGO Jean — 3500 FCFA/LITRE"],
        )

    def test_switch_detected_from_a_producer_menu(self):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_product_switch,
        )

        sw = detect_buyer_product_switch(self._menu_state(), "je veux acheter des tomates")
        assert sw is not None and sw.new_product == "tomates"

    @pytest.mark.parametrize("text", [
        "je veux celui de Gilbert-prod", "je veux celui de OUEDRAOGO Jean", "je veux le premier",
        "je veux acheter du lait", "je veux du lait", "2",
    ])
    def test_menu_references_and_same_product_are_not_switches(self, text):
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_product_switch,
        )

        assert detect_buyer_product_switch(self._menu_state(), text) is None, text

    def test_generic_selection_menu_of_another_flow_is_untouched(self):
        # Un SELECTION_MENU générique (commandes, enchères…) sans contexte de tunnel panier.
        from ladini.graphs.agents.market_coach.core.pending_interaction import (
            InteractionKind,
            set_pending_interaction,
        )
        from ladini.graphs.agents.market_coach.interpreter.product_switch import (
            detect_buyer_product_switch,
        )
        from tests.conftest import make_state

        st = make_state(
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={"product": "poulets"},
            **set_pending_interaction(InteractionKind.SELECTION_MENU, context_ref="ui_menu"),
        )
        assert detect_buyer_product_switch(st, "je veux acheter du lait") is None


class TestSwitchDuringTierMenu:
    @staticmethod
    def _tiered_lait():
        o = _offer("lait", "LITRE", "Ferme0", "L0")
        o["pricing_tiers"] = [
            {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 500.0, "packaging": "bidon",
             "base_unit_quantity": 5.0, "min_order_quantity": 1},
            {"tier_id": "t10", "quantity": 10.0, "unit": "L", "price": 900.0, "packaging": "bidon",
             "base_unit_quantity": 10.0, "min_order_quantity": 1},
        ]
        return o

    @pytest.mark.parametrize("variant", ["invalid_json", "unknown", "hallucinated_qty"])
    def test_new_purchase_leaves_the_tier_menu(self, variant):
        c = _Conv([_POULETS, self._tiered_lait()] + _TOMATES, _HOSTILE[variant])
        c.say("je veux acheter des poulets")
        st = c.say("je veux acheter du lait")
        assert "conditionnements" in st["final_response"], st["final_response"]
        c.llm.families.clear()
        st = c.say("je voudrais des tomates")
        _not_trapped(st, c, "tomates")
        assert "structured" not in c.llm.families
        assert not st.get("tier_selection_context") or st["tier_selection_context"].get("__reset__")
        assert st["active_cart"] == []
