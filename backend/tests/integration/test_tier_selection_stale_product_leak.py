"""Real live incident (2026-08-31): a buyer who had already purchased
"lait" (milk, sold in 5L/10L tiers) later searched for "poulets" (chickens,
flat price, `pricing_tiers=None`). `vendor_selection_context` correctly
refreshed to the poulets vendors, but `tier_selection_context` — a
`replace_value` channel nothing ever explicitly cleared — still held the
milk tiers from the earlier, already-completed purchase. Once a poulets
vendor was picked, the tier menu was shown again with the OLD milk data
("5.0 l (bidon) — 500 FCFA") under the label "*poulets*", and the reply the
buyer gave next had nowhere sensible to go.

Two independent fixes are proven here:
1. `CartDomainService.build_product_selection_menu` (multi-vendor search)
   now resets `tier_selection_context` to `None` whenever it seeds a fresh
   vendor menu — the exact code path this incident went through live.
2. Both tier-resolution sites in `cart.py` (`vendor_ctx_active` and the
   single-vendor path) now refuse to trust a `tier_selection_context` whose
   `product_id` doesn't match the product currently being resolved — closing
   the bug class even for any site that might forget the explicit reset.
"""
from __future__ import annotations

from typing import Any, Dict

from agriconnect.graphs.agents.market_coach.core.pending_interaction import (
    get_pending_interaction,
    to_tunnel_category,
)
from agriconnect.graphs.agents.market_coach.flows.buyer.cart import cart_management
from tests.conftest import StubRuntime, run


def _poulets_vendor(product_id: str = "P-POULETS") -> Dict[str, Any]:
    return {
        "product_id": product_id,
        "name": "poulets",
        "price": 3000.0,
        "unit": "UNITE",
        "vendor_name": "jojo",
        "producer_id": "PR-JOJO",
        "source_type": "DIRECT",
        "is_auction": False,
        "pricing_tiers": None,  # flat price — exactly the real DB row
    }


def _stale_lait_tier_context() -> Dict[str, Any]:
    """Reproduces EXACTLY the leaked checkpoint state read from the live
    incident's workspace row: `product_id: null`, milk's 5L/10L tiers."""
    return {
        "product_id": None,
        "tiers": [
            {"tier_id": "t5", "quantity": 5.0, "unit": "l", "price": 500.0, "packaging": "bidon"},
            {"tier_id": "t10", "quantity": 10.0, "unit": "l", "price": 900.0, "packaging": "bidon"},
        ],
    }


class TestStaleTierContextNeverLeaksAcrossProducts:
    def test_multi_vendor_search_resets_stale_tier_context(self, monkeypatch):
        """The exact live code path: a fresh multi-vendor search for a
        DIFFERENT, untiered product must not let an old tier menu survive."""
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_poulets_vendor(), _poulets_vendor("P-POULETS-2")], True

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        runtime = StubRuntime()
        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "NONE",
            "status": "PROCESSING",
            "normalized_text": "je veux des poulets",
            "user_query": "je veux des poulets",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "poulets", "quantity": 1, "unit": "UNITE"},
            "working_memory": {},
            "active_cart": [
                {
                    "product_id": "P-LAIT",
                    "name": "lait",
                    "quantity": 11.0,
                    "unit": "LITRE",
                    "price": 500.0,
                    "line_total": 5500.0,
                    "status": "VALIDATED",
                }
            ],
            # The exact leaked state from the live incident.
            "tier_selection_context": _stale_lait_tier_context(),
        }

        result = run(cart_management(state, runtime))

        assert result.get("tier_selection_context") is None, (
            "a fresh vendor search must clear any stale tier_selection_context"
        )
        assert to_tunnel_category(get_pending_interaction(result)) == "SELECTION"
        assert "poulets" in (result.get("final_response") or "").lower()
        assert "bidon" not in (result.get("final_response") or "").lower(), (
            f"the stale milk tier menu must never surface: {result.get('final_response')!r}"
        )

    def test_vendor_ctx_active_branch_ignores_a_stale_tier_context_for_a_different_product(
        self,
    ):
        """Reproduces the exact turn that broke live: vendor_selection_context
        already correctly resolved to poulets (chosen_vendor set, flat
        price, no tiers), but tier_selection_context is still the OLD milk
        one. Picking the vendor must add straight to cart — no phantom tier
        menu, no data from an unrelated product."""
        runtime = StubRuntime()
        vendor = _poulets_vendor()
        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "SELECTION",
            "status": "WAITING_INPUT",
            "interpreted_event": "SELECTION",
            "normalized_text": "1",
            "user_query": "1",
            "user_phone": "+22601479800",
            "transaction_payload": {
                "product": "poulets",
                "quantity": 1,
                "unit": "UNITE",
                "selection_index": 1,
            },
            "working_memory": {},
            "active_cart": [],
            "vendor_selection_context": {
                "product": "poulets",
                "vendors": [vendor, _poulets_vendor("P-POULETS-2")],
                "available_mapping_kind": "product_vendor",
                "requested_quantity": 1,
                "requested_unit": "UNITE",
            },
            "tier_selection_context": _stale_lait_tier_context(),
        }

        result = run(cart_management(state, runtime))

        assert result["status"] == "COMPLETED", (
            f"expected a direct add-to-cart (flat-price product, no real tiers) — "
            f"status={result['status']} final_response={result.get('final_response')!r}"
        )
        line = result["active_cart"][-1]
        assert line["product_id"] == "P-POULETS"
        assert "tier_id" not in line or line.get("tier_id") is None

    def test_single_vendor_path_ignores_a_stale_tier_context_for_a_different_product(
        self, monkeypatch
    ):
        import agriconnect.graphs.agents.market_coach.services.domain.cart_service as cart_service_mod

        async def _fake_resolve_vendors(self, phone, product_name):
            return [_poulets_vendor()], False

        monkeypatch.setattr(
            cart_service_mod.CartDomainService,
            "resolve_product_vendors",
            _fake_resolve_vendors,
        )

        runtime = StubRuntime()
        state: Dict[str, Any] = {
            "current_goal": "BUYER_ADD_TO_CART",
            "expected_input": "NONE",
            "status": "PROCESSING",
            "normalized_text": "je veux 2 poulets",
            "user_query": "je veux 2 poulets",
            "user_phone": "+22601479800",
            "transaction_payload": {"product": "poulets", "quantity": 2, "unit": "UNITE"},
            "working_memory": {},
            "active_cart": [],
            "tier_selection_context": _stale_lait_tier_context(),
        }

        result = run(cart_management(state, runtime))

        assert result["status"] == "COMPLETED", (
            f"status={result['status']} final_response={result.get('final_response')!r}"
        )
        line = result["active_cart"][-1]
        assert line["product_id"] == "P-POULETS"
        assert "tier_id" not in line or line.get("tier_id") is None
