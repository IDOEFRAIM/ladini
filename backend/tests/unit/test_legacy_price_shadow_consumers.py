"""`Product.price` d'un produit à paliers = LEGACY SHADOW (prix brut du 1er palier, imposé par NOT NULL).

Audit exhaustif des consommateurs (2026-09-29) : chacun de ces tests verrouille qu'un consommateur qui
lisait le shadow comme un prix commercial ne le fait plus. `pricing_tiers` est la vérité commerciale.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from ladini.services.database import buyer as buyer_mod
from ladini.services.database.errors import BusinessRuleException
from tests.conftest import run
from tests.unit.test_create_preorder_draft_pricing_tiers import (
    _FakeSession,
    _service,
    _tiered_product,
)

TIERS = [
    {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 700.0, "packaging": "bidon",
     "base_unit_quantity": 5.0, "min_order_quantity": 1},
    {"tier_id": "t9", "quantity": 9.0, "unit": "L", "price": 1000.0, "packaging": "bidon",
     "base_unit_quantity": 9.0, "min_order_quantity": 1},
]


def _miel(**over):
    return _tiered_product(name="miel", price=700.0, quantity_for_sale=60.0, pricing_tiers=TIERS, **over)


class TestBuyerSearchNeverRanksOnTheShadow:
    def test_sort_key_of_an_uncertified_tiered_row_is_last(self):
        tiered = {"price": 1.0, "pricing_tiers": TIERS, "commercial_pricing": None, "normalized_unit_price": None}
        plain = {"price": 900.0, "pricing_tiers": None, "commercial_pricing": None, "normalized_unit_price": None}
        assert buyer_mod._sort_price(tiered) > buyer_mod._sort_price(plain)

    def test_a_certified_package_product_keeps_its_normalized_rank(self):
        certified = {"price": 1000.0, "pricing_tiers": TIERS, "commercial_pricing": {"schema_version": 1},
                     "normalized_unit_price": 140.0}
        assert buyer_mod._sort_price(certified) == 140.0

    def test_sql_ordering_pushes_tiered_products_after_priced_ones(self):
        sql = str(buyer_mod._is_tiered_sql().compile(dialect=postgresql.dialect()))
        assert "jsonb_array_length" in sql and "commercial_pricing IS NULL" in sql


class TestNoOrderIsEverPricedFromTheShadow:
    def test_preorder_of_a_tiered_product_without_a_tier_is_refused_not_billed_700(self):
        product = _miel()
        with pytest.raises(BusinessRuleException):
            run(
                _service(_FakeSession(product)).create_preorder_draft(
                    buyer_phone="+22670000001",
                    cart_items=[{"product_id": str(product.id), "quantity": 2}],
                )
            )

    def test_preorder_with_the_chosen_tier_is_still_priced_by_the_tier(self):
        product = _miel()
        session = _FakeSession(product)
        result = run(
            _service(session).create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[{"product_id": str(product.id), "quantity": 2, "tier_id": "t9"}],
            )
        )
        assert result["total_amount"] == 2000.0, "2 x 1000, jamais 2 x 700 (shadow)"
        (item,) = [o for o in session.added if type(o).__name__ == "OrderItem"]
        assert item.base_unit_quantity == 18.0

    def test_untiered_product_is_unchanged(self):
        product = _tiered_product(pricing_tiers=None, price=900.0)
        result = run(
            _service(_FakeSession(product)).create_preorder_draft(
                buyer_phone="+22670000001", cart_items=[{"product_id": str(product.id), "quantity": 3}],
            )
        )
        assert result["total_amount"] == 2700.0


class TestNegotiationDoesNotQuoteTheShadowAsACatalogPrice:
    def _negotiate(self, product):
        product.sub_category_id = uuid.uuid4()
        svc = _service(_FakeSession(product))
        return run(
            svc.initiate_negotiation_session(
                buyer_phone="+22670000001", product_id=str(product.id), offered_price=100.0, quantity=10.0
            )
        )

    def test_tiered_product_has_no_seller_minimum_nor_price_gap(self):
        res = self._negotiate(_miel())
        assert res["seller_minimum"] is None and res["price_gap"] is None
        assert "prix catalogue" not in res["message"]

    def test_untiered_product_keeps_its_reference_price(self):
        res = self._negotiate(_tiered_product(pricing_tiers=None, price=900.0))
        assert res["seller_minimum"] == 900.0 and res["price_gap"] == 800.0


class TestMarketStatisticsAndMatchingExcludeTheShadow:
    def test_market_aggregates_filter(self):
        from ladini.services.database.category import _has_comparable_price

        sql = str(_has_comparable_price().compile(dialect=postgresql.dialect()))
        assert "jsonb_array_length" in sql and "commercial_pricing IS NOT NULL" in sql

    def test_recurring_matching_candidates_exclude_uncertified_tiered_products(self):
        from ladini.workers.automation.need_matching_service import _CANDIDATES_SQL

        text = str(_CANDIDATES_SQL)
        assert "commercial_pricing IS NOT NULL" in text and "jsonb_array_length" in text


class TestFirmOrderRefusesATieredProductWithoutATier:
    def test_finalize_multi_order_never_bills_the_shadow(self):
        import types

        product = _miel()
        product.sub_category_id = None
        svc = _service(_FakeSession(product))
        buyer = types.SimpleNamespace(id=uuid.uuid4(), name="A", zone_id=uuid.uuid4())

        async def _profile(phone=None):
            return buyer, types.SimpleNamespace(id=uuid.uuid4(), establishment_name=None)

        svc.get_buyer_profile = _profile
        with pytest.raises(BusinessRuleException) as exc:
            run(svc.finalize_multi_order([{"product_id": str(product.id), "quantity": 2}], "+22670000001"))
        assert getattr(exc.value, "reason", None) == "tier_required" or "conditionnement" in str(exc.value)
