"""B16 — inventaire par conditionnement : débit/restitution/refus par variante, invariants, Decimal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ladini.domain.package_inventory import (
    PackageInventoryError,
    check_variant_availability,
    debit_stock_for_item,
    effective_available_count,
    packaged_total,
    restore_stock_for_item,
    validate_inventory_invariant,
)
from ladini.domain.pricing_tiers import (
    PricingTierError,
    tiers_to_dicts,
    validate_pricing_tiers,
)


def _tier(tid, size, count, price, packaging="bidon", unit="LITRE"):
    return {
        "tier_id": tid, "quantity": size, "unit": unit, "price": price, "packaging": packaging,
        "base_unit_quantity": size, "min_order_quantity": 1, "available_count": count,
    }


def _product(qty, tiers):
    return SimpleNamespace(id="p1", name="Gapal", unit="LITRE", quantity_for_sale=qty, pricing_tiers=tiers)


def _item(tid, n, size):
    return SimpleNamespace(tier_id=tid, quantity=n, base_unit_quantity=n * size)


def _mixed():
    return _product(58.0, [_tier("t500", 0.5, 50, 500), _tier("t330", 0.33, 100, 400)] )


def test_invariant_ok_and_decimal_exact():
    p = _mixed()
    # 50*0.5 + 100*0.33 = 58 exactement (Decimal, pas de dérive flottante)
    assert str(packaged_total(p.pricing_tiers)) == "58.00"
    validate_inventory_invariant(p.pricing_tiers, 58.0)


def test_invariant_mismatch_and_partial_refused():
    with pytest.raises(PackageInventoryError) as e:
        validate_inventory_invariant(_mixed().pricing_tiers, 50.0)
    assert e.value.reason == "package_inventory_mismatch"
    tiers = [_tier("a", 0.5, 50, 500), {**_tier("b", 0.33, 1, 400)}]
    del tiers[1]["available_count"]
    with pytest.raises(PackageInventoryError) as e:
        validate_inventory_invariant(tiers, 25.0)
    assert e.value.reason == "partial_package_inventory"


def test_no_counts_is_historical_noop():
    validate_inventory_invariant([{"packaging": "sac", "quantity": 25}], 999)


def test_oversell_refused_even_when_global_litres_suffice():
    p = _mixed()
    refusal = debit_stock_for_item(p, _item("t500", 60, 0.5))  # 30 L <= 58 L mais 60 > 50 bidons
    assert refusal and refusal["reason"] == "insufficient_package_stock"
    assert refusal["available_packages"] == 50
    assert p.quantity_for_sale == 58.0 and p.pricing_tiers[0]["available_count"] == 50  # aucune mutation


def test_mandate_scenario_successive_sales():
    p = _mixed()
    assert debit_stock_for_item(p, _item("t500", 10, 0.5)) is None
    assert debit_stock_for_item(p, _item("t330", 20, 0.33)) is None
    counts = {t["tier_id"]: t["available_count"] for t in p.pricing_tiers}
    assert counts == {"t500": 40, "t330": 80}
    assert float(p.quantity_for_sale) == pytest.approx(46.4)
    refusal = check_variant_availability(p, "t500", 41)
    assert refusal and refusal["available_packages"] == 40


def test_restore_is_symmetric():
    p = _mixed()
    item = _item("t500", 10, 0.5)
    debit_stock_for_item(p, item)
    restore_stock_for_item(p, item)
    assert p.pricing_tiers[0]["available_count"] == 50 and float(p.quantity_for_sale) == pytest.approx(58.0)


def test_100_sachets_buy_5():
    p = _product(50.0, [_tier("s", 0.5, 100, 500, packaging="sachet")])
    assert debit_stock_for_item(p, _item("s", 5, 0.5)) is None
    assert p.pricing_tiers[0]["available_count"] == 95 and float(p.quantity_for_sale) == pytest.approx(47.5)


def test_drift_never_sells_more_than_physical():
    p = _mixed()
    p.quantity_for_sale = 10.0  # un autre chemin a baissé le stock physique seul
    assert effective_available_count(p, p.pricing_tiers[0]) == 20
    assert check_variant_availability(p, "t500", 21) is not None


def test_tier_without_packaging_keeps_historical_physical_debit():
    p = _product(50.0, [{"tier_id": "x", "quantity": 10, "unit": "LITRE", "price": 900, "base_unit_quantity": 10}])
    assert debit_stock_for_item(p, SimpleNamespace(tier_id="x", quantity=1, base_unit_quantity=10)) is None
    assert float(p.quantity_for_sale) == 40.0
    assert debit_stock_for_item(p, SimpleNamespace(tier_id=None, quantity=45, base_unit_quantity=None)) is not None


def test_simple_base_unit_regression():
    p = _product(50.0, [])
    assert debit_stock_for_item(p, SimpleNamespace(tier_id=None, quantity=5, base_unit_quantity=5)) is None
    assert float(p.quantity_for_sale) == 45.0


def test_mass_sacs_regression():
    p = SimpleNamespace(id="m", name="Maïs", unit="KG", quantity_for_sale=500.0,
                        pricing_tiers=[_tier("sac", 25, 20, 15000, packaging="sac", unit="KG")])
    assert debit_stock_for_item(p, _item("sac", 3, 25)) is None
    assert p.pricing_tiers[0]["available_count"] == 17 and float(p.quantity_for_sale) == 425.0


def test_canonical_identity_dedupe_sachet_500ml_equals_half_litre():
    base = {"packaging": "sachet", "price": 500, "available_count": 10}
    with pytest.raises(PricingTierError):
        validate_pricing_tiers(
            [{**base, "quantity": 500, "unit": "ML"}, {**base, "quantity": 0.5, "unit": "LITRE"}], base_unit="LITRE"
        )


def test_sachet_and_bidon_same_size_are_distinct_and_count_roundtrips():
    raw = [
        {"packaging": "sachet", "quantity": 0.5, "unit": "LITRE", "price": 500, "available_count": 10},
        {"packaging": "bidon", "quantity": 0.5, "unit": "LITRE", "price": 700, "available_count": 4},
    ]
    out = tiers_to_dicts(validate_pricing_tiers(raw, base_unit="LITRE"))
    assert [t["available_count"] for t in out] == [10, 4]
