"""`flows/buyer/cart.py::cart_management` + `services/domain/cart_service.py`
— two real bugs reported from a live WhatsApp conversation (2026-08-17):

1. Replying to the vendor menu (by digit or ordinal) while quantity was
   still unfilled was silently absorbed by the "missing product or
   quantity" guard, which ran BEFORE vendor re-selection. `chosen_vendor`
   was never updated on that turn, and a later vendor switch was applied
   retroactively with zero acknowledgment to the buyer once quantity
   finally arrived (`selection_index` stayed "sticky", per
   `nodes/memory.py`'s own documented `mapping_kind == "product_vendor"`
   contract — this file exercises that the vendor block now actually
   consumes it promptly, restoring that contract).
2. The buyer's stated unit (e.g. "45 kg") was never forwarded to
   `add_to_cart_with_ref` — the cart line silently used the CATALOG
   listing's own unit instead (e.g. "TONNE"), applying the raw quantity
   number against the wrong unit — a ~1000x mispricing risk.
"""
from __future__ import annotations

import pytest

from ladini.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.flows.buyer.cart import cart_management
from tests.conftest import StubRuntime, make_state, run


def rt(responses=None):
    return StubRuntime(responses=responses or {})


def _vendor(vendor_name, unit, price, producer_id, product_id="P-1"):
    return {
        "product_id": product_id,
        "name": "tomates",
        "price": price,
        "unit": unit,
        "vendor_name": vendor_name,
        "producer_id": producer_id,
        "source_type": "DIRECT",
        "is_auction": False,
    }


JOJO = _vendor("jojo", "KG", 225, "PR-JOJO", "P-JOJO")
ARSENE_TONNE = _vendor("Arsene TOUGMA", "TONNE", 10000, "PR-ARSENE", "P-ARS-T")
ARSENE_KG = _vendor("Arsene TOUGMA", "KG", 10000, "PR-ARSENE", "P-ARS-K")


# =====================================================================
# Bug 2 — unit mismatch between buyer's stated unit and the vendor listing
# =====================================================================

class TestAddToCartUnitHandling:
    def test_buyer_unit_matching_the_listing_unit_is_unaffected(self):
        from ladini.graphs.agents.market_coach.services.domain.cart_service import (
            CartDomainService,
        )
        svc = CartDomainService(rt())
        result = run(svc.add_to_cart_with_ref(
            "+22670000001", "tomates", 45, JOJO, [], make_state(), buyer_unit="KG",
        ))
        line = result["active_cart"][-1]
        assert line["quantity"] == 45.0
        assert line["unit"] == "KG"

    def test_buyer_says_kg_vendor_sells_by_tonne_converts_instead_of_mispricing(self):
        """The exact reported bug: buyer types '45 kg', matched listing is
        priced per TONNE. Must convert 45 kg -> 0.045 TONNE, NOT apply 45
        directly against the TONNE price (which would silently 1000x the
        bill: 450 FCFA vs the wrong 450 000 FCFA)."""
        from ladini.graphs.agents.market_coach.services.domain.cart_service import (
            CartDomainService,
        )
        svc = CartDomainService(rt(responses={
            "validate_stock_availability_atomic": {
                "status": "success", "unit_price": 10000, "unit": "TONNE",
                "available_quantity": 200,
            },
        }))
        result = run(svc.add_to_cart_with_ref(
            "+22670000001", "tomates", 45, ARSENE_TONNE, [], make_state(),
            buyer_unit="KG",
        ))
        line = result["active_cart"][-1]
        assert line["quantity"] == pytest.approx(0.045)
        assert line["unit"] == "TONNE"
        assert line["line_total"] == pytest.approx(450.0)

    def test_no_buyer_unit_given_falls_back_to_listing_unit_unchanged(self):
        """Regression guard: when the buyer never states a unit (the common
        case — most messages are just a bare number), behavior must be
        IDENTICAL to before this fix."""
        from ladini.graphs.agents.market_coach.services.domain.cart_service import (
            CartDomainService,
        )
        svc = CartDomainService(rt(responses={
            "validate_stock_availability_atomic": {
                "status": "success", "unit_price": 10000, "unit": "TONNE",
                "available_quantity": 200,
            },
        }))
        result = run(svc.add_to_cart_with_ref(
            "+22670000001", "tomates", 45, ARSENE_TONNE, [], make_state(),
            buyer_unit=None,
        ))
        line = result["active_cart"][-1]
        assert line["quantity"] == 45.0
        assert line["unit"] == "TONNE"

    def test_buyer_unit_with_no_universal_conversion_asks_for_clarification_instead_of_guessing(self):
        """SAC has no fixed kg-equivalent — must never silently guess."""
        from ladini.graphs.agents.market_coach.services.domain.cart_service import (
            CartDomainService,
        )
        svc = CartDomainService(rt())
        result = run(svc.add_to_cart_with_ref(
            "+22670000001", "tomates", 3, ARSENE_KG, [], make_state(),
            buyer_unit="SAC",
        ))
        assert result["status"] == "WAITING_INPUT"
        assert to_tunnel_category(get_pending_interaction(result)) == "QUANTITY"
        assert "kg" in result["final_response"].lower()


# =====================================================================
# Bug 1 — vendor re-selection must run BEFORE the quantity guard, and a
# real switch must be acknowledged.
# =====================================================================

class TestCartManagementVendorReselection:
    def _menu_state(self, **overrides):
        base = dict(
            current_goal="BUYER_ADD_TO_CART",
            transaction_payload={},
            vendor_selection_context={
                "product": "tomates",
                "vendors": [JOJO, ARSENE_TONNE, ARSENE_KG],
                "chosen_vendor": None,
                "requested_quantity": None,
                "requested_unit": None,
                "available_mapping_kind": "product_vendor",
            },
            active_cart=[],
        )
        base.update(overrides)
        return make_state(**base)

    def test_first_reply_to_the_menu_resolves_chosen_vendor_on_the_same_turn(self):
        """Bug: this used to be swallowed by the quantity guard and never
        resolve chosen_vendor until a LATER turn. Must resolve immediately."""
        state = self._menu_state(transaction_payload={"selection_index": 1})
        result = run(cart_management(state, rt()))
        assert result["vendor_selection_context"]["chosen_vendor"]["vendor_name"] == "jojo"
        assert "jojo" in result["final_response"]

    def test_switching_vendor_before_giving_quantity_is_acknowledged(self):
        """The exact reported bug: user picks '1', then changes their mind to
        '2' before ever giving a quantity. Must (a) actually switch, and
        (b) tell the user it switched."""
        state = self._menu_state(transaction_payload={"selection_index": 1})
        first = run(cart_management(state, rt()))
        state.update(first)

        state["transaction_payload"] = {"selection_index": 2}
        second = run(cart_management(state, rt()))

        assert second["vendor_selection_context"]["chosen_vendor"]["vendor_name"] == "Arsene TOUGMA"
        assert second["vendor_selection_context"]["chosen_vendor"]["unit"] == "TONNE"
        assert "changement" in second["final_response"].lower()
        assert "Arsene TOUGMA" in second["final_response"]

    def test_reselecting_the_same_vendor_again_is_not_reported_as_a_switch(self):
        state = self._menu_state(transaction_payload={"selection_index": 1})
        first = run(cart_management(state, rt()))
        state.update(first)

        state["transaction_payload"] = {"selection_index": 1}
        second = run(cart_management(state, rt()))
        assert "changement" not in second["final_response"].lower()

    def test_full_conversation_ends_with_correct_unit_and_price(self):
        """End-to-end: menu -> pick #1 -> switch to #2 -> give quantity in a
        DIFFERENT unit than #2's listing. Regression for both bugs together,
        matching the real reported conversation."""
        state = self._menu_state(transaction_payload={"selection_index": 1})
        state.update(run(cart_management(state, rt())))

        state["transaction_payload"] = {"selection_index": 2}
        state.update(run(cart_management(state, rt())))

        state["transaction_payload"] = {"quantity": 45, "unit": "KG"}
        final = run(cart_management(
            state,
            rt(responses={
                "validate_stock_availability_atomic": {
                    "status": "success", "unit_price": 10000, "unit": "TONNE",
                    "available_quantity": 200,
                },
            }),
        ))
        cart_line = final["active_cart"][-1]
        assert cart_line["vendor_name"] == "Arsene TOUGMA"
        assert cart_line["unit"] == "TONNE"
        assert cart_line["quantity"] == pytest.approx(0.045)
        assert cart_line["line_total"] == pytest.approx(450.0)

