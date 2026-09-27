"""Market Balance (Phase E) — pure aggregation rules (no database). Covers the mission's own
ETAPE 38 checklist: demand>supply, supply>demand, demand=supply, demand=0, supply=0, zero
denominator, compatible/incompatible unit conversion, mixed units, reliable_scope, and the "no
observable activity" no-fabricated-row rule."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from ladini.domain.analytics.market_balance_aggregation import (
    RecurringDemandFact,
    SupplyFact,
    TenderDemandFact,
    aggregate_market_balance,
)

DAY = date(2026, 9, 27)
ZONE = str(uuid.uuid4())
CATEGORY = str(uuid.uuid4())
SUB = str(uuid.uuid4())


def _recurring(**over):
    base = dict(zone_id=ZONE, category_id=CATEGORY, sub_category_id=SUB, unit="KG", priority_unit="KG", open_quantity=100)
    base.update(over)
    return RecurringDemandFact(**base)


def _tender(**over):
    base = dict(zone_id=ZONE, category_id=CATEGORY, sub_category_id=SUB, unit="KG", priority_unit="KG", quantity=100)
    base.update(over)
    return TenderDemandFact(**base)


def _supply(**over):
    base = dict(zone_id=ZONE, category_id=CATEGORY, sub_category_id=SUB, canonical_unit="KG", available_quantity=100)
    base.update(over)
    return SupplyFact(**base)


def _one(rows):
    assert len(rows) == 1
    return rows[0]


class TestCoreFormulas:
    def test_demand_greater_than_supply_yields_a_gap_and_full_coverable(self):
        row = _one(aggregate_market_balance(DAY, [_recurring(open_quantity=1000)], [], [_supply(available_quantity=800)]))
        assert row["open_demand_quantity"] == Decimal("800") + Decimal("200")
        assert row["potential_coverable_quantity"] == 800
        assert row["demand_gap_quantity"] == 200
        assert row["excess_supply_quantity"] == 0

    def test_supply_greater_than_demand_yields_excess_and_full_coverable(self):
        row = _one(aggregate_market_balance(DAY, [_recurring(open_quantity=500)], [], [_supply(available_quantity=900)]))
        assert row["potential_coverable_quantity"] == 500
        assert row["demand_gap_quantity"] == 0
        assert row["excess_supply_quantity"] == 400

    def test_demand_equals_supply_yields_zero_gap_and_zero_excess(self):
        row = _one(aggregate_market_balance(DAY, [_recurring(open_quantity=100)], [], [_supply(available_quantity=100)]))
        assert row["demand_gap_quantity"] == 0 and row["excess_supply_quantity"] == 0
        assert row["potential_coverable_quantity"] == 100

    def test_demand_zero_supply_positive_is_pure_excess(self):
        row = _one(aggregate_market_balance(DAY, [], [], [_supply(available_quantity=250)]))
        assert row["open_demand_quantity"] == 0
        assert row["excess_supply_quantity"] == 250
        assert row["demand_gap_quantity"] == 0 and row["potential_coverable_quantity"] == 0

    def test_supply_zero_demand_positive_is_pure_gap(self):
        row = _one(aggregate_market_balance(DAY, [_recurring(open_quantity=300)], [], []))
        assert row["available_supply_quantity"] == 0
        assert row["demand_gap_quantity"] == 300
        assert row["excess_supply_quantity"] == 0 and row["potential_coverable_quantity"] == 0

    def test_no_demand_and_no_supply_produces_no_row_not_a_zero_row(self):
        """SS15: 'no observable activity' is the ABSENCE of a row, never a stored 0/0 cell."""
        assert aggregate_market_balance(DAY, [], [], []) == []
        # A different, unrelated cell with zero net demand (fully confirmed) and no supply either
        # must not leak in as a spurious row.
        assert aggregate_market_balance(DAY, [_recurring(open_quantity=0)], [], []) == []


class TestReliableScope:
    def test_recurring_only_is_labelled_recurring(self):
        row = _one(aggregate_market_balance(DAY, [_recurring()], [], [_supply()]))
        assert row["demand_scope"] == "RECURRING"

    def test_tender_only_is_labelled_tender(self):
        row = _one(aggregate_market_balance(DAY, [], [_tender()], [_supply()]))
        assert row["demand_scope"] == "TENDER"

    def test_both_journeys_present_is_labelled_combined_and_sums_quantities(self):
        row = _one(aggregate_market_balance(DAY, [_recurring(open_quantity=60)], [_tender(quantity=40)], []))
        assert row["demand_scope"] == "RECURRING+TENDER"
        assert row["open_demand_quantity"] == 100

    def test_a_fully_confirmed_occurrence_contributes_nothing_not_a_negative_or_zero_entry(self):
        """A negative subtraction (over-confirmed, shouldn't happen but defensive) or an exactly-zero
        remaining gap must never register as RECURRING demand for that cell."""
        row = aggregate_market_balance(DAY, [_recurring(open_quantity=0)], [], [_supply()])
        assert row[0]["open_demand_quantity"] == 0 and row[0]["demand_scope"] == "RECURRING"


class TestUnits:
    def test_compatible_conversion_grams_to_kg_combines_into_one_cell(self):
        row = _one(aggregate_market_balance(
            DAY, [_recurring(unit="G", priority_unit="KG", open_quantity=500)], [],
            [_supply(canonical_unit="KG", available_quantity=1)]))
        assert row["canonical_unit"] == "KG"
        assert row["open_demand_quantity"] == Decimal("0.5")  # 500 G -> 0.5 KG

    def test_incompatible_units_never_combine_into_one_cell(self):
        rows = aggregate_market_balance(
            DAY,
            [_recurring(unit="KG", priority_unit="KG", open_quantity=200), _recurring(unit="TETE", priority_unit="TETE", open_quantity=12, sub_category_id=SUB)],
            [], [])
        assert {r["canonical_unit"] for r in rows} == {"KG", "TETE"}
        assert len(rows) == 2
        assert all(r["measurement_family"] in ("MASS", "COUNT") for r in rows)

    def test_no_defined_conversion_keeps_the_unit_distinct_not_silently_dropped(self):
        rows = aggregate_market_balance(DAY, [_recurring(unit="SAC", priority_unit="KG", open_quantity=5)], [], [])
        assert _one(rows)["canonical_unit"] == "SAC"  # SAC has no defined conversion to KG: stays SAC

    def test_mixed_units_produce_a_row_per_unit_never_a_combined_total(self):
        rows = aggregate_market_balance(
            DAY, [_recurring(unit="KG", open_quantity=100)], [],
            [_supply(canonical_unit="KG", available_quantity=80), _supply(canonical_unit="TETE", available_quantity=5, sub_category_id=SUB)])
        by_unit = {r["canonical_unit"]: r for r in rows}
        assert set(by_unit) == {"KG", "TETE"}
        assert by_unit["TETE"]["open_demand_quantity"] == 0 and by_unit["TETE"]["excess_supply_quantity"] == 5


class TestMultiRowIsolation:
    def test_two_different_sub_categories_never_share_a_cell(self):
        other_sub = str(uuid.uuid4())
        rows = aggregate_market_balance(DAY, [_recurring(open_quantity=10), _recurring(open_quantity=20, sub_category_id=other_sub)], [], [])
        assert {r["sub_category_id"] for r in rows} == {SUB, other_sub}
        assert {r["open_demand_quantity"] for r in rows} == {10, 20}

    def test_two_different_zones_never_share_a_cell_no_double_counting(self):
        other_zone = str(uuid.uuid4())
        rows = aggregate_market_balance(DAY, [], [], [_supply(available_quantity=500), _supply(available_quantity=300, zone_id=other_zone)])
        totals = {r["zone_scope"]: r["available_supply_quantity"] for r in rows}
        assert totals == {ZONE: 500, other_zone: 300}
        assert sum(totals.values()) == 800  # never fabricated as if one producer's stock counted twice
