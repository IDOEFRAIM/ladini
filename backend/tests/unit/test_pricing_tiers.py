"""Domain layer for multi-tier pricing — see `domain/pricing_tiers.py`.

This module is now the SOLE validation point for `pricing_tiers` (replaces
the ad-hoc per-field sanitizing that used to live directly in
`services/database/producer.py::create_product`). These tests lock in the
contract: reject-whole-payload on any anomaly, never a partial/guessed
result.
"""
from __future__ import annotations

import pytest

from agriconnect.domain.pricing_tiers import (
    ComputedLine,
    PricingTier,
    PricingTierError,
    compute_line,
    resolve_stock_debit,
    resolve_tier,
    tiers_to_dicts,
    validate_pricing_tiers,
)


class TestValidatePricingTiers:
    def test_empty_or_none_returns_empty_list(self):
        assert validate_pricing_tiers(None, "LITRE") == []
        assert validate_pricing_tiers([], "LITRE") == []

    def test_valid_tiers_are_parsed_with_stable_tier_ids(self):
        raw = [
            {"quantity": 5, "unit": "L", "price": 500, "packaging": "bidon"},
            {"quantity": 10, "unit": "l", "price": 900, "packaging": "bidon"},
        ]
        tiers = validate_pricing_tiers(raw, "LITRE")
        assert len(tiers) == 2
        assert all(isinstance(t, PricingTier) for t in tiers)
        assert {t.tier_id for t in tiers} == {tiers[0].tier_id, tiers[1].tier_id}
        assert len({t.tier_id for t in tiers}) == 2, "chaque tarif doit avoir un tier_id unique"

    def test_base_unit_quantity_defaults_to_literal_quantity_for_same_unit(self):
        tiers = validate_pricing_tiers(
            [{"quantity": 5, "unit": "L", "price": 500}], "LITRE"
        )
        assert tiers[0].base_unit_quantity == 5.0

    def test_mass_family_conversion_tonne_to_kg_base(self):
        tiers = validate_pricing_tiers(
            [{"quantity": 2, "unit": "TONNE", "price": 200000}], "KG"
        )
        assert tiers[0].base_unit_quantity == 2000.0

    def test_mixed_unit_family_is_rejected(self):
        """Incident réel évité par cette règle : rien n'empêchait avant un
        producteur de mélanger un tarif en KG et un en LITRE pour le même
        produit — la quantité totale dérivée (memory.py) n'aurait plus eu de
        sens (`somme des quantités de tiers` en additionnant des unités
        incompatibles)."""
        with pytest.raises(PricingTierError, match="incompatible"):
            validate_pricing_tiers(
                [{"quantity": 5, "unit": "KG", "price": 500}], "LITRE"
            )

    def test_singleton_family_unit_must_match_base_exactly(self):
        # SAC et PANIER sont deux familles singleton distinctes (aucune
        # conversion connue) — un produit en SAC ne peut pas avoir de tarif
        # en PANIER.
        with pytest.raises(PricingTierError, match="incompatible"):
            validate_pricing_tiers(
                [{"quantity": 5, "unit": "PANIER", "price": 500}], "SAC"
            )

    def test_duplicate_tier_is_rejected(self):
        raw = [
            {"quantity": 5, "unit": "L", "price": 500, "packaging": "bidon"},
            {"quantity": 5, "unit": "L", "price": 600, "packaging": "bidon"},
        ]
        with pytest.raises(PricingTierError, match="double"):
            validate_pricing_tiers(raw, "LITRE")

    def test_non_dict_tier_is_rejected(self):
        with pytest.raises(PricingTierError, match="objet"):
            validate_pricing_tiers(["not a dict"], "LITRE")

    def test_missing_unit_is_rejected(self):
        with pytest.raises(PricingTierError, match="unité manquante"):
            validate_pricing_tiers([{"quantity": 5, "price": 500}], "LITRE")

    def test_non_positive_price_is_rejected(self):
        with pytest.raises(PricingTierError):
            validate_pricing_tiers(
                [{"quantity": 5, "unit": "L", "price": 0}], "LITRE"
            )

    def test_non_positive_quantity_is_rejected(self):
        with pytest.raises(PricingTierError):
            validate_pricing_tiers(
                [{"quantity": -1, "unit": "L", "price": 500}], "LITRE"
            )

    def test_whole_payload_rejected_on_a_single_bad_tier(self):
        """Une seule entrée invalide invalide TOUT le payload — jamais de
        publication partielle avec certains tarifs acceptés et d'autres
        silencieusement jetés (exigence explicite utilisateur)."""
        raw = [
            {"quantity": 5, "unit": "L", "price": 500, "packaging": "bidon"},
            {"quantity": 10, "unit": "L", "price": -900, "packaging": "bidon"},
        ]
        with pytest.raises(PricingTierError):
            validate_pricing_tiers(raw, "LITRE")

    def test_no_base_unit_is_rejected(self):
        with pytest.raises(PricingTierError, match="unité de base"):
            validate_pricing_tiers(
                [{"quantity": 5, "unit": "L", "price": 500}], ""
            )


class TestTiersToDicts:
    def test_round_trips_through_dict_form(self):
        tiers = validate_pricing_tiers(
            [{"quantity": 5, "unit": "L", "price": 500, "packaging": "bidon"}],
            "LITRE",
        )
        dicts = tiers_to_dicts(tiers)
        assert dicts[0]["quantity"] == 5.0
        assert dicts[0]["packaging"] == "bidon"
        assert "tier_id" in dicts[0]
        assert "base_unit_quantity" in dicts[0]


class TestResolveTierAndComputeLine:
    def _two_tiers_raw(self):
        tiers = validate_pricing_tiers(
            [
                {"quantity": 5, "unit": "L", "price": 500, "packaging": "bidon"},
                {"quantity": 10, "unit": "L", "price": 900, "packaging": "bidon"},
            ],
            "LITRE",
        )
        return tiers_to_dicts(tiers)

    def test_resolve_tier_finds_by_id(self):
        raw = self._two_tiers_raw()
        tier = resolve_tier(raw, raw[1]["tier_id"])
        assert tier.quantity == 10.0
        assert tier.price == 900.0

    def test_resolve_tier_missing_id_raises(self):
        raw = self._two_tiers_raw()
        with pytest.raises(PricingTierError, match="introuvable"):
            resolve_tier(raw, "not-a-real-id")

    def test_compute_line_multiplies_by_pack_count(self):
        raw = self._two_tiers_raw()
        tier = resolve_tier(raw, raw[1]["tier_id"])  # 10L bidon @ 900
        line = compute_line(tier, 3)
        assert isinstance(line, ComputedLine)
        assert line.price_total == 2700.0
        assert line.base_unit_quantity == 30.0

    def test_compute_line_enforces_minimum_order_quantity(self):
        raw = validate_pricing_tiers(
            [{"quantity": 10, "unit": "L", "price": 900, "min_order_quantity": 2}],
            "LITRE",
        )
        tier = raw[0]
        with pytest.raises(PricingTierError, match="minimale"):
            compute_line(tier, 1)
        # 2 packs satisfies the threshold.
        line = compute_line(tier, 2)
        assert line.base_unit_quantity == 20.0


class TestResolveStockDebit:
    class _FakeOrderItem:
        def __init__(self, quantity, base_unit_quantity=None):
            self.quantity = quantity
            self.base_unit_quantity = base_unit_quantity

    def test_uses_base_unit_quantity_when_present(self):
        item = self._FakeOrderItem(quantity=3, base_unit_quantity=30.0)
        assert resolve_stock_debit(item) == 30.0

    def test_falls_back_to_quantity_when_no_tier(self):
        """Commande sans palier (produit sans pricing_tiers, ou commande
        créée avant cette refonte) : comportement STRICTEMENT identique à
        avant — débite `quantity` directement."""
        item = self._FakeOrderItem(quantity=5, base_unit_quantity=None)
        assert resolve_stock_debit(item) == 5.0
