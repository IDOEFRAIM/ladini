"""Phase D — metric layer rules with a canned-rows session (the real SQL runs against PostgreSQL
in tests/schema/test_analytics_metric_layer_pg.py, in CI)."""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from ladini.domain.analytics.metric_dictionary import METRICS
from ladini.domain.analytics.metric_layer import (
    BINDINGS,
    UNAVAILABLE,
    DataStatus,
    TargetRow,
    TargetStatus,
    compute_value,
    evaluate_target,
    resolve_target,
)
from tests.conftest import run

START, END = date(2026, 9, 1), date(2026, 9, 7)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return SimpleNamespace(all=lambda: self._rows, one=lambda: self._rows[0])

    def scalar(self):
        return next(iter(self._rows[0].values()))


class FakeSession:
    """`handler(sql, params) -> list[dict]`."""

    def __init__(self, handler):
        self.handler = handler
        self.statements = []

    async def execute(self, clause, params=None):
        sql = str(clause)
        self.statements.append(sql)
        return _Rows(self.handler(sql, params or {}))


def _service(handler, today=date(2026, 12, 1)):
    from ladini.services.analytics.analytics_service import AnalyticsService

    return AnalyticsService(FakeSession(handler), today=today)


def _no_targets(sql, params, rows):
    return [] if "metric_targets" in sql else rows


class TestWeightedRates:
    def test_weighted_not_averaged(self):
        # Two groups: 800/1000 and 20/100 -> 820/1100 (74.5%), NOT the mean of 80% and 20% (= 50%).
        assert compute_value(800 + 20, 1000 + 100) == pytest.approx(820 / 1100)
        assert compute_value(800 + 20, 1000 + 100) != pytest.approx(0.5)

    def test_zero_denominator_is_none_not_zero(self):
        assert compute_value(0, 0) is None and compute_value(5, 0) is None

    def test_zero_numerator_with_positive_denominator_is_a_real_zero(self):
        assert compute_value(0, 10) == 0.0


class TestServiceValues:
    def test_ratio_from_summed_parts(self):
        svc = _service(lambda sql, p: _no_targets(sql, p, [{"num": 820, "den": 1100, "n_rows": 2}]))
        res = run(svc.get_metric("tender_response_rate", START, END, compare=False))
        assert res.numerator == 820 and res.denominator == 1100 and res.value == pytest.approx(820 / 1100)

    def test_no_rows_is_no_data(self):
        svc = _service(lambda sql, p: [])
        res = run(svc.get_metric("tender_response_rate", START, END, compare=False))
        assert res.status == DataStatus.NO_DATA and res.value is None

    def test_zero_denominator_is_no_data_with_null_value(self):
        svc = _service(lambda sql, p: _no_targets(sql, p, [{"num": 0, "den": 0, "n_rows": 3}]))
        res = run(svc.get_metric("tender_response_rate", START, END, compare=False))
        assert res.value is None and res.status == DataStatus.NO_DATA

    def test_mixed_units_never_yield_a_global_physical_number(self):
        rows = [{"canonical_unit": "KG", "num": 100, "den": 200, "n_rows": 1}, {"canonical_unit": "L", "num": 50, "den": 50, "n_rows": 1}]
        svc = _service(lambda sql, p: _no_targets(sql, p, rows))
        res = run(svc.get_metric("recurring_coverage_rate", START, END, compare=False))
        assert res.status == DataStatus.MIXED_UNITS
        assert res.value is None and res.numerator is None and res.denominator is None
        assert {b["canonical_unit"]: b["value"] for b in res.breakdown} == {"KG": 0.5, "L": 1.0}

    def test_single_unit_returns_the_physical_sums(self):
        rows = [{"canonical_unit": "KG", "num": 4310, "den": 6240, "n_rows": 5}]
        svc = _service(lambda sql, p: _no_targets(sql, p, rows))
        res = run(svc.get_metric("recurring_coverage_rate", START, END, compare=False))
        assert res.value == pytest.approx(4310 / 6240) and res.numerator == 4310 and res.denominator == 6240

    def test_previous_period_and_delta(self):
        answers = iter([[{"num": 69, "den": 100, "n_rows": 1}], [{"num": 63, "den": 100, "n_rows": 1}]])

        def handler(sql, p):
            return [] if "metric_targets" in sql else next(answers)

        res = run(_service(handler).get_metric("tender_response_rate", START, END))
        assert res.value == pytest.approx(0.69) and res.previous_value == pytest.approx(0.63) and res.delta == pytest.approx(0.06)

    def test_unsupported_filter_is_rejected_not_ignored(self):
        svc = _service(lambda sql, p: [])
        with pytest.raises(ValueError):
            run(svc.get_metric("needs_created", START, END, filters={"category_id": str(uuid.uuid4())}, compare=False))


class TestUnavailable:
    @pytest.mark.parametrize("name", sorted(UNAVAILABLE))
    def test_unavailable_metrics_return_null_with_the_reason_and_never_query(self, name):
        svc = _service(lambda sql, p: pytest.fail("must not query"))
        res = run(svc.get_metric(name, START, END))
        assert res.status == DataStatus.UNAVAILABLE and res.value is None and UNAVAILABLE[name] in res.notes

    def test_the_documented_unavailable_set(self):
        assert {"recurring_delivered_quantity", "recurring_fulfillment_rate", "direct_fulfillment_rate",
                "fulfillment_rate", "recurring_modification_rate"} <= set(UNAVAILABLE)

    def test_every_binding_and_unavailable_name_exists_in_the_dictionary(self):
        assert set(BINDINGS) <= set(METRICS) and set(UNAVAILABLE) <= set(METRICS)


class TestNorthStar:
    def _svc(self, sums):
        return _service(lambda sql, p: _no_targets(sql, p, [sums]), today=date(2026, 12, 1))

    def test_spr_exposes_partial_status_journey_components_and_the_reliable_scope(self):
        sums = dict(nd=10, nt=5, nr=20, sd=6, st=2, sr=4)
        res = run(self._svc(sums).get_metric("successful_procurement_rate", START, END, compare=False))
        assert res.status == DataStatus.PARTIAL
        assert res.numerator == 12 and res.denominator == 35 and res.value == pytest.approx(12 / 35)
        by = {b["journey"]: b for b in res.breakdown}
        assert by["RECURRING"]["reliability"] == "PARTIAL"
        assert by["DIRECT+TENDER (reliable_scope)"]["value"] == pytest.approx(8 / 15)
        assert any("lower bound" in n for n in res.notes)  # never presented as a complete delivery rate

    def test_spr_flags_immature_cohorts(self):
        res = run(_service(lambda sql, p: _no_targets(sql, p, [dict(nd=1, nt=0, nr=0, sd=0, st=0, sr=0)]),
                           today=date(2026, 9, 8)).get_metric("successful_procurement_rate", START, END, compare=False))
        assert any("younger than" in n for n in res.notes)

    def test_spr_without_needs_is_null(self):
        res = run(self._svc(dict(nd=0, nt=0, nr=0, sd=0, st=0, sr=0)).get_metric("successful_procurement_rate", START, END, compare=False))
        assert res.value is None and res.status == DataStatus.NO_DATA


class TestUnfulfilledDemand:
    def test_is_named_unmatched_and_reports_undelivered_as_unavailable(self):
        rows = [{"canonical_unit": "KG", "measurement_family": "MASS", "requested": 100, "matched": 60, "unmatched": 40}]
        out = run(_service(lambda sql, p: rows).get_unfulfilled_demand(START, END))
        assert out["kind"] == "UNMATCHED_DEMAND"
        assert out["rows"][0]["unmatched"] == 40 and out["rows"][0]["unmatched_ratio"] == pytest.approx(0.4)
        assert "quantity_delivered" in out["undelivered_demand"]


def _t(scope, sid, value, **kw):
    return TargetRow(scope, sid, value, kw.get("warning"), kw.get("critical"), kw.get("valid_from", date(2026, 1, 1)), kw.get("valid_until"))


class TestTargetResolution:
    SUB, CAT, ZONE = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

    def test_most_specific_applicable_scope_wins(self):
        cands = [_t("GLOBAL", None, 0.5), _t("JOURNEY", "RECURRING", 0.55), _t("ZONE", self.ZONE, 0.6),
                 _t("CATEGORY", self.CAT, 0.7), _t("SUBCATEGORY", self.SUB, 0.8)]
        on = date(2026, 9, 1)
        assert resolve_target(cands, on=on, sub_category_id=self.SUB, category_id=self.CAT, zone_id=self.ZONE, journey="RECURRING").target_value == 0.8
        assert resolve_target(cands, on=on, category_id=self.CAT, zone_id=self.ZONE, journey="RECURRING").target_value == 0.7
        assert resolve_target(cands, on=on, zone_id=self.ZONE, journey="RECURRING").target_value == 0.6
        assert resolve_target(cands, on=on, journey="RECURRING").target_value == 0.55
        assert resolve_target(cands, on=on).target_value == 0.5

    def test_scope_not_filtered_on_never_applies(self):
        assert resolve_target([_t("ZONE", self.ZONE, 0.6)], on=date(2026, 9, 1)) is None
        assert resolve_target([_t("CATEGORY", self.CAT, 0.7)], on=date(2026, 9, 1), category_id=str(uuid.uuid4())) is None

    def test_validity_window_and_latest_effective_row(self):
        cands = [_t("GLOBAL", None, 0.4, valid_from=date(2026, 1, 1), valid_until=date(2026, 6, 30)),
                 _t("GLOBAL", None, 0.5, valid_from=date(2026, 7, 1)), _t("GLOBAL", None, 0.45, valid_from=date(2026, 8, 1))]
        assert resolve_target(cands, on=date(2026, 5, 1)).target_value == 0.4
        assert resolve_target(cands, on=date(2026, 9, 1)).target_value == 0.45
        assert resolve_target(cands, on=date(2025, 12, 31)) is None

    def test_no_target_means_no_target_status(self):
        assert evaluate_target(0.9, None) == TargetStatus.NO_TARGET

    def test_status_bands_higher_is_better(self):
        t = _t("GLOBAL", None, 0.5, warning=0.4, critical=0.3)
        assert [evaluate_target(v, t) for v in (0.6, 0.45, 0.35, 0.2)] == [
            TargetStatus.ON_TARGET, TargetStatus.BELOW_TARGET, TargetStatus.WARNING, TargetStatus.CRITICAL]

    def test_status_bands_lower_is_better(self):
        t = _t("GLOBAL", None, 0.10, warning=0.20, critical=0.30)
        assert [evaluate_target(v, t, higher_is_better=False) for v in (0.05, 0.15, 0.25, 0.4)] == [
            TargetStatus.ON_TARGET, TargetStatus.BELOW_TARGET, TargetStatus.WARNING, TargetStatus.CRITICAL]

    def test_service_attaches_the_resolved_target_and_status(self):
        def handler(sql, p):
            if "metric_targets" in sql:
                return [{"scope_type": "GLOBAL", "scope_id": None, "target_value": 0.6, "warning_threshold": 0.5,
                         "critical_threshold": 0.4, "valid_from": date(2026, 1, 1), "valid_until": None}]
            return [{"num": 45, "den": 100, "n_rows": 1}]

        res = run(_service(handler).get_metric("tender_response_rate", START, END, compare=False))
        assert res.target["value"] == 0.6 and res.target_status == TargetStatus.WARNING
