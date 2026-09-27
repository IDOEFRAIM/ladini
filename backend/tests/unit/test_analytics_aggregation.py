"""domain/analytics/aggregation.py — weighted-rate and compatible-quantity
summing. Covers mission section 19.C/D: the 800/1000 + 20/100 -> 820/1100
worked example, and explicit rejection (never silent drop) of incompatible
units.
"""
from __future__ import annotations

import pytest

from ladini.domain.analytics.aggregation import (
    IncompatibleUnitsError,
    aggregate_compatible_quantities,
    assert_compatible_or_raise,
    weighted_rate,
)


class TestWeightedRate:
    def test_worked_example_from_the_mission(self):
        # Tomate: 800/1000 = 80% ; Oignon: 20/100 = 20% ; Global: 820/1100,
        # NEVER (0.80 + 0.20) / 2 = 50%.
        rate = weighted_rate([(800, 1000), (20, 100)])
        assert rate.numerator == 820
        assert rate.denominator == 1100
        assert rate.value == pytest.approx(820 / 1100)
        assert rate.value != pytest.approx((0.8 + 0.2) / 2)

    def test_recurring_coverage_worked_example(self):
        # Mission section 19.C exact figures: requested=425, matched=100.
        rate = weighted_rate([(100, 425)])
        assert rate.value == pytest.approx(100 / 425)

    def test_zero_denominator_is_undefined_not_zero(self):
        rate = weighted_rate([(0, 0)])
        assert rate.value is None

    def test_empty_parts_is_undefined(self):
        rate = weighted_rate([])
        assert rate.numerator == 0
        assert rate.denominator == 0
        assert rate.value is None


class TestAggregateCompatibleQuantities:
    def test_sums_compatible_pairs_with_conversion(self):
        result = aggregate_compatible_quantities(
            [(1000, "G"), (2, "KG")], canonical_unit="KG"
        )
        assert result.total == pytest.approx(3.0)
        assert result.canonical_unit == "KG"
        assert result.rejected == ()

    def test_incompatible_pair_is_rejected_not_dropped_silently(self):
        result = aggregate_compatible_quantities(
            [(3, "KG"), (4, "LITRE")], canonical_unit="KG"
        )
        assert result.total == pytest.approx(3.0)
        assert result.rejected == ((4, "LITRE"),)

    def test_all_incompatible_returns_none_total(self):
        result = aggregate_compatible_quantities([(4, "LITRE")], canonical_unit="KG")
        assert result.total is None
        assert result.rejected == ((4, "LITRE"),)


class TestAssertCompatibleOrRaise:
    def test_compatible_pair_does_not_raise(self):
        assert_compatible_or_raise("KG", "TONNE")

    def test_incompatible_pair_raises_explicitly(self):
        # Mission test D: "MASS + VOLUME -> erreur/segmentation explicite."
        with pytest.raises(IncompatibleUnitsError):
            assert_compatible_or_raise("KG", "LITRE")
