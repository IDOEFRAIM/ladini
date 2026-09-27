"""Phase E closing — the ops CLI dispatches to the right service function and prints only a plain
JSON summary (no session object, no secret, no raw row) — this is what a CI job log captures.

`worker_session` is always faked here (never the real DB engine): this suite must stay a unit test
that never needs `DATABASE_URL` (real-Postgres coverage lives in tests/schema/)."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from ladini.services.analytics import ops_cli
from tests.conftest import run


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        pass

    async def execute(self, *_a, **_k):
        class _Result:
            def scalar(self):
                return 3

        return _Result()


def _fake_worker_session():
    return _FakeSession()


def test_recompute_calls_recompute_recent_with_the_given_lookback():
    with patch.object(ops_cli, "recompute_recent", AsyncMock(return_value={"2026-09-01": {}})) as m:
        out = run(ops_cli.cmd_recompute(14))
    m.assert_awaited_once_with(ops_cli.worker_session, 14)
    assert out == {"action": "recompute", "lookback_days": 14, "days_recomputed": 1, "per_day_rows": {"2026-09-01": {}}}


def test_backfill_calls_the_phase_d5_writer_and_reports_a_count():
    with patch.object(ops_cli, "worker_session", _fake_worker_session), \
         patch.object(ops_cli, "backfill_quantity_delivered", AsyncMock(return_value=7)) as m:
        out = run(ops_cli.cmd_backfill())
    m.assert_awaited_once()
    assert out == {"action": "backfill", "occurrences_updated": 7}


def test_drift_counts_via_the_same_join_the_writer_uses():
    with patch.object(ops_cli, "worker_session", _fake_worker_session):
        out = run(ops_cli.cmd_drift())
    assert out == {"action": "drift", "occurrences_diverging": 3}


def test_quality_reports_counts_only_never_row_detail_text():
    from ladini.services.analytics.data_quality import QualityIssue

    issue = QualityIssue("unknown_canonical_unit", "analytics.recurring_daily_metrics", "WARNING", 2, "BIDON, SEAU")
    with patch.object(ops_cli, "worker_session", _fake_worker_session), \
         patch.object(ops_cli, "run_data_quality_checks", AsyncMock(return_value=[issue])):
        out = run(ops_cli.cmd_quality(30))
    assert out["issue_count"] == 1
    assert out["issues"] == [{"check": "unknown_canonical_unit", "table": "analytics.recurring_daily_metrics", "severity": "WARNING", "count": 2}]
    assert "detail" not in out["issues"][0]  # only counts leave this CLI, never free-text row detail


def test_verify_kpis_flags_a_mismatch_between_dashboard_and_source(monkeypatch):
    from ladini.domain.analytics.metric_layer import MetricResult

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalar(self):
            return self._rows[0][0]

        def mappings(self):
            return self

        def one(self):
            return self._rows[0]

        def all(self):
            return self._rows

    # Une seule ligne par requête source, dans l'ordre d'appel de cmd_verify_kpis (active_buyers,
    # needs_created, search events, tender response, recurring coverage raw rows, unmatched demand).
    answers = iter([
        _Result([(3,)]),  # active_buyers source
        _Result([(9999,)]),  # needs_created source — volontairement DIFFÉRENT du dashboard
        _Result([{"performed": 10, "succeeded": 4}]),
        _Result([{"created": 5, "with_bid": 2}]),
        _Result([{"sub_category_id": "s1", "unit": "KG", "requested_quantity": 100, "quantity_matched": 60, "priority_unit": "KG"}]),
        _Result([(15,)]),  # unmatched demand source, matches the dashboard value below
    ])

    class _Session:
        async def execute(self, *_a, **_k):
            return next(answers)

        async def scalar(self, *_a, **_k):
            return next(answers).scalar()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    def _metric(name, **over):
        base = dict(metric_name=name, value=3, numerator=3, denominator=None, unit=None, status="OK", period={})
        base.update(over)
        return MetricResult(**base)

    async def fake_get_metric(self, name, start, end, *, compare=True, filters=None):
        return {
            "active_buyers": _metric("active_buyers", value=3, unit="buyers"),
            "needs_created": _metric("needs_created", value=3, unit="count"),  # dashboard=3, source=9999 -> mismatch
            "direct_search_success_rate": _metric("direct_search_success_rate", value=0.4, unit="ratio"),
            "tender_response_rate": _metric("tender_response_rate", value=0.4, unit="ratio"),
        }[name]

    async def fake_breakdown(self, name, start, end, *, dimension, filters=None):
        return [{"sub_category_id": "s1", "canonical_unit": "KG", "numerator": 60.0, "denominator": 100.0, "value": 0.6}]

    async def fake_unmatched(self, start, end, *, filters=None, group_by=()):
        return {"rows": [{"sub_category_id": "s1", "canonical_unit": "KG", "unmatched": 15.0}]}

    monkeypatch.setattr(ops_cli.AnalyticsService, "get_metric", fake_get_metric)
    monkeypatch.setattr(ops_cli.AnalyticsService, "get_metric_breakdown", fake_breakdown)
    monkeypatch.setattr(ops_cli.AnalyticsService, "get_unfulfilled_demand", fake_unmatched)
    with patch.object(ops_cli, "worker_session", lambda: _Session()):
        out = run(ops_cli.cmd_verify_kpis(14))

    by_metric = {c["metric"]: c for c in out["checks"]}
    assert by_metric["active_buyers"]["match"] is True
    assert by_metric["needs_created"]["match"] is False  # 3 != 9999, surfaced, never silently ignored
    assert by_metric["recurring_coverage_rate"]["match"] is True
    assert by_metric["unmatched_demand"]["match"] is True
    assert out["all_match"] is False  # one mismatch is enough to fail the whole check


def test_verify_kpis_recurring_coverage_compares_per_group_not_a_loose_total(monkeypatch):
    """Regression: an earlier version summed the WHOLE dashboard breakdown against a source query
    restricted to one unit, so a real conversion (G->KG) inflated the dashboard total unnoticed. This
    proves the check is now per (sub_category, canonical_unit) group, not a loose grand total."""
    from ladini.domain.analytics.metric_layer import MetricResult

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalar(self):
            return self._rows[0][0]

        def mappings(self):
            return self

        def one(self):
            return self._rows[0]

        def all(self):
            return self._rows

    answers = iter([
        _Result([(0,)]), _Result([(0,)]), _Result([{"performed": 0, "succeeded": 0}]), _Result([{"created": 0, "with_bid": 0}]),
        # Raw rows: two occurrences of the SAME sub-category, one already in KG, one in G (converted to KG).
        _Result([
            {"sub_category_id": "s1", "unit": "KG", "requested_quantity": 100, "quantity_matched": 60, "priority_unit": "KG"},
            {"sub_category_id": "s1", "unit": "G", "requested_quantity": 2000, "quantity_matched": 1000, "priority_unit": "KG"},
        ]),
        _Result([(0,)]),
    ])

    class _Session:
        async def execute(self, *_a, **_k):
            return next(answers)

        async def scalar(self, *_a, **_k):
            return next(answers).scalar()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    def _metric(name, **over):
        base = dict(metric_name=name, value=None, numerator=None, denominator=None, unit=None, status="NO_DATA", period={})
        base.update(over)
        return MetricResult(**base)

    async def fake_get_metric(self, name, start, end, *, compare=True, filters=None):
        return _metric(name)

    async def fake_unmatched(self, start, end, *, filters=None, group_by=()):
        return {"rows": []}

    # Dashboard only reflects the FIRST occurrence (bug scenario): 60/100, missing the G->KG one (1/2 KG).
    async def fake_breakdown_missing_conversion(self, name, start, end, *, dimension, filters=None):
        return [{"sub_category_id": "s1", "canonical_unit": "KG", "numerator": 60.0, "denominator": 100.0, "value": 0.6}]

    monkeypatch.setattr(ops_cli.AnalyticsService, "get_metric", fake_get_metric)
    monkeypatch.setattr(ops_cli.AnalyticsService, "get_metric_breakdown", fake_breakdown_missing_conversion)
    monkeypatch.setattr(ops_cli.AnalyticsService, "get_unfulfilled_demand", fake_unmatched)
    with patch.object(ops_cli, "worker_session", lambda: _Session()):
        out = run(ops_cli.cmd_verify_kpis(14))

    coverage = next(c for c in out["checks"] if c["metric"] == "recurring_coverage_rate")
    assert coverage["match"] is False
    assert coverage["group_mismatches"][0]["source"] == {"requested": 102.0, "matched": 61.0}


def test_main_prints_exactly_one_json_line(capsys):
    with patch.object(ops_cli, "worker_session", _fake_worker_session), \
         patch.object(ops_cli, "backfill_quantity_delivered", AsyncMock(return_value=0)), \
         patch("sys.argv", ["ops_cli", "backfill"]):
        ops_cli.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert json.loads(out[0]) == {"action": "backfill", "occurrences_updated": 0}
