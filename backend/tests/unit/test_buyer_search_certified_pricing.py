"""Buyer search results carry CERTIFIED commercial pricing, not a raw `price`/`unit` guess
(Phase B2c.4).

`search_products` (services/database/buyer.py) mixes catalog (`Product`) and future-production
(`MarketOffer`) rows. Before this phase, both were sorted/displayed by a raw `price` number,
silently assuming "price per unit" — exactly the bug already closed for the write paths in
B1/B2c.3, now closed for this READ path. These tests exercise the pure helpers
(`_pricing_fields`/`_sort_price`) and the underlying `*_pricing_view` functions directly: no DB
needed, `search_products` itself is covered by the pre-existing harness/integration tests.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from ladini.domain.commercial_offer import PriceBasis
from ladini.domain.commercial_pricing_snapshot import (
    CommercialPricingSnapshot,
    market_offer_pricing_view,
    product_pricing_view,
)
from ladini.services.database.buyer import _pricing_fields, _sort_price

D = Decimal


def _total_lot_snapshot(amount: float, *, inventory_amount: float, inventory_unit: str) -> CommercialPricingSnapshot:
    return CommercialPricingSnapshot(
        commercial_price_amount=D(str(amount)),
        price_basis=PriceBasis.TOTAL_LOT,
        inventory_quantity_amount=D(str(inventory_amount)),
        inventory_quantity_unit=inventory_unit,
    ).with_normalized()


def _per_base_unit_snapshot(amount: float, *, price_unit: str, inventory_amount: float, inventory_unit: str) -> CommercialPricingSnapshot:
    return CommercialPricingSnapshot(
        commercial_price_amount=D(str(amount)),
        price_basis=PriceBasis.PER_BASE_UNIT,
        price_unit=price_unit,
        inventory_quantity_amount=D(str(inventory_amount)),
        inventory_quantity_unit=inventory_unit,
    ).with_normalized()


class TestGoldenBTotalLotNeverShownAsPerUnit:
    def test_market_offer_total_lot_label_is_for_the_whole_lot(self):
        snap = _total_lot_snapshot(4_000_000, inventory_amount=10000, inventory_unit="KG")
        row = {"price_per_unit": 4_000_000.0, "pricing_snapshot": snap.to_dict()}
        view = market_offer_pricing_view(row)
        fields = _pricing_fields(view)
        assert fields["price_basis"] == "TOTAL_LOT"
        assert fields["certification_status"] == "CERTIFIED"
        assert "pour l'ensemble" in fields["pricing_label"]
        # Jamais le libellé principal "400000/TONNE" ou "400000/KG" :
        assert "/ KG" not in fields["pricing_label"] and "/ TONNE" not in fields["pricing_label"]

    def test_product_total_lot_label_is_for_the_whole_lot(self):
        snap = _total_lot_snapshot(50000, inventory_amount=50, inventory_unit="KG")
        product = SimpleNamespace(price=D("50000"), commercial_pricing=snap.to_dict())
        fields = _pricing_fields(product_pricing_view(product))
        assert fields["price_basis"] == "TOTAL_LOT"
        assert "pour l'ensemble" in fields["pricing_label"]


class TestGoldenFRankingNeverMixesRawBases:
    def test_total_lot_and_per_base_unit_are_compared_on_the_normalized_value(self):
        """A = 450000/TONNE (=450 FCFA/KG) ; B = 4.2M TOTAL_LOT sur 10000 kg (=420 FCFA/KG).
        B est normalement MOINS cher au kg malgre un montant brut 9x plus grand — le tri doit
        refleter cela, jamais comparer 450000 (brut A) a 4200000 (brut B)."""
        a_snap = _per_base_unit_snapshot(450000, price_unit="TONNE", inventory_amount=10000, inventory_unit="KG")
        b_snap = _total_lot_snapshot(4_200_000, inventory_amount=10000, inventory_unit="KG")
        a = _pricing_fields(market_offer_pricing_view({"price_per_unit": 450000.0, "pricing_snapshot": a_snap.to_dict()}))
        b = _pricing_fields(market_offer_pricing_view({"price_per_unit": 4_200_000.0, "pricing_snapshot": b_snap.to_dict()}))
        a["price"] = 450000.0
        b["price"] = 4_200_000.0
        assert a["is_comparable"] and b["is_comparable"]
        assert _sort_price(b) < _sort_price(a)  # B (420/kg) moins cher que A (450/kg)
        # Le brut, lui, dirait le contraire (4.2M > 450k) — c'est exactement le piege ferme ici.
        assert b["price"] > a["price"]


class TestLegacyNeverInventsABasis:
    def test_legacy_market_offer_is_uncertified_not_silently_per_unit(self):
        fields = _pricing_fields(market_offer_pricing_view({"price_per_unit": 400000.0, "pricing_snapshot": None}))
        assert fields["certification_status"] == "LEGACY_PARTIAL"
        assert fields["price_basis"] == "UNKNOWN"
        assert fields["normalized_unit_price"] is None
        assert not fields["is_comparable"]
        assert "non certifi" in fields["pricing_label"]

    def test_legacy_product_is_never_comparable(self):
        product = SimpleNamespace(price=D("1000"), commercial_pricing=None)
        fields = _pricing_fields(product_pricing_view(product))
        assert not fields["is_comparable"]
        assert fields["certification_status"] == "LEGACY_PARTIAL"

    def test_no_price_at_all_says_so_rather_than_zero(self):
        fields = _pricing_fields(market_offer_pricing_view({"price_per_unit": None, "pricing_snapshot": None}))
        assert fields["pricing_label"] == "Prix non disponible"
