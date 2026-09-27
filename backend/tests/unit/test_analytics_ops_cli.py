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


def test_main_prints_exactly_one_json_line(capsys):
    with patch.object(ops_cli, "worker_session", _fake_worker_session), \
         patch.object(ops_cli, "backfill_quantity_delivered", AsyncMock(return_value=0)), \
         patch("sys.argv", ["ops_cli", "backfill"]):
        ops_cli.main()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert json.loads(out[0]) == {"action": "backfill", "occurrences_updated": 0}
