"""Market Balance (Phase E) — service-layer unit tests (no database): the row contract's coverage
math, filter validation, and the grouped demand-gaps/excess-supply views (grouping is what keeps
incompatible units from ever being ranked together, §26/§27)."""
from __future__ import annotations

import uuid

import pytest

from ladini.services.analytics.market_balance_service import (
    DEMAND_RELIABILITY,
    BalanceRow,
    MarketBalanceService,
)
from tests.conftest import run

ZONE = str(uuid.uuid4())
CATEGORY = str(uuid.uuid4())
SUB = str(uuid.uuid4())


def _row(**over):
    base = dict(zone_scope=ZONE, category_id=CATEGORY, sub_category_id=SUB, canonical_unit="KG",
                open_demand_quantity=100.0, available_supply_quantity=80.0, potential_coverable_quantity=80.0,
                demand_gap_quantity=20.0, excess_supply_quantity=0.0, demand_scope="RECURRING", as_of="2026-09-27")
    base.update(over)
    return BalanceRow(**base)


class TestBalanceRowContract:
    def test_coverage_rate_is_the_ratio_never_a_percentage_pre_multiplied(self):
        d = _row().as_dict()
        assert d["potential_coverage_rate"] == pytest.approx(0.8)

    def test_zero_open_demand_yields_null_coverage_not_zero_division(self):
        d = _row(open_demand_quantity=0.0, potential_coverable_quantity=0.0, excess_supply_quantity=50.0, demand_gap_quantity=0.0).as_dict()
        assert d["potential_coverage_rate"] is None

    def test_reliability_mapping_matches_demand_scope(self):
        assert _row(demand_scope="RECURRING").as_dict()["demand_reliability"] == "RELIABLE"
        assert _row(demand_scope="TENDER").as_dict()["demand_reliability"] == "PARTIAL"
        assert _row(demand_scope="RECURRING+TENDER").as_dict()["demand_reliability"] == "PARTIAL"
        assert _row().as_dict()["supply_reliability"] == "RELIABLE"

    def test_tender_scope_carries_an_explicit_caveat_note_recurring_only_does_not(self):
        assert _row(demand_scope="RECURRING").as_dict()["notes"] == []
        assert "cancelled-after-award" in _row(demand_scope="TENDER").as_dict()["notes"][0]

    def test_demand_reliability_table_has_no_hidden_journey(self):
        assert set(DEMAND_RELIABILITY) == {"RECURRING", "TENDER", "RECURRING+TENDER"}


class TestFilterValidation:
    def test_unknown_filter_key_is_rejected(self):
        with pytest.raises(ValueError, match="not a Market Balance dimension"):
            MarketBalanceService._where({"journey": "DIRECT"})

    def test_a_list_filter_becomes_any(self):
        where, params = MarketBalanceService._where({"zone_scope": [ZONE]})
        assert "= ANY(:f_zone_scope)" in where
        assert params["f_zone_scope"] == [uuid.UUID(ZONE)]

    def test_none_values_are_skipped(self):
        where, params = MarketBalanceService._where({"category_id": None})
        assert where == "" and params == {}


class TestGroupedViews:
    def _svc_with_fake_balance(self, monkeypatch, rows):
        svc = MarketBalanceService(session=object())

        async def fake_current_balance(*, filters=None):
            return {"status": "OK", "as_of": "2026-09-27", "rows": rows}

        monkeypatch.setattr(svc, "get_current_balance", fake_current_balance)
        return svc

    def test_demand_gaps_are_grouped_by_unit_and_sorted_descending_within_each_group(self, monkeypatch):
        rows = [
            _row(canonical_unit="KG", demand_gap_quantity=50.0).as_dict(),
            _row(canonical_unit="KG", demand_gap_quantity=200.0).as_dict(),
            _row(canonical_unit="TETE", demand_gap_quantity=5.0).as_dict(),
            _row(canonical_unit="KG", demand_gap_quantity=0.0).as_dict(),  # no gap: excluded
        ]
        svc = self._svc_with_fake_balance(monkeypatch, rows)
        out = run(svc.get_demand_gaps())
        groups = {g["canonical_unit"]: g["rows"] for g in out["groups"]}
        assert set(groups) == {"KG", "TETE"}
        assert [r["demand_gap_quantity"] for r in groups["KG"]] == [200.0, 50.0]
        assert len(groups["TETE"]) == 1

    def test_excess_supply_is_grouped_by_unit_and_sorted_descending(self, monkeypatch):
        rows = [
            _row(canonical_unit="KG", excess_supply_quantity=10.0, demand_gap_quantity=0.0).as_dict(),
            _row(canonical_unit="KG", excess_supply_quantity=90.0, demand_gap_quantity=0.0).as_dict(),
            _row(canonical_unit="KG", excess_supply_quantity=0.0).as_dict(),  # no excess: excluded
        ]
        svc = self._svc_with_fake_balance(monkeypatch, rows)
        out = run(svc.get_excess_supply())
        assert [r["excess_supply_quantity"] for r in out["groups"][0]["rows"]] == [90.0, 10.0]

    def test_unavailable_balance_propagates_as_empty_groups_not_a_crash(self, monkeypatch):
        svc = MarketBalanceService(session=object())

        async def fake_current_balance(*, filters=None):
            return {"status": "UNAVAILABLE", "as_of": None, "rows": [], "notes": ["No snapshot yet."]}

        monkeypatch.setattr(svc, "get_current_balance", fake_current_balance)
        out = run(svc.get_demand_gaps())
        assert out["status"] == "UNAVAILABLE" and out["groups"] == []
