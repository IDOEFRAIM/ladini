"""domain/analytics/metric_targets.py — design-contract validation only
(mission 19.I: target scope validation). No database is involved: this
table does not exist yet (see module docstring)."""
from __future__ import annotations

from datetime import date

import pytest

from ladini.domain.analytics.metric_targets import (
    EXAMPLE_TARGETS,
    MetricTarget,
    TargetScopeType,
)


class TestMetricTargetValidation:
    def test_rejects_unknown_metric_name(self):
        with pytest.raises(KeyError):
            MetricTarget(
                metric_name="not_a_real_metric",
                scope_type=TargetScopeType.GLOBAL,
                scope_id=None,
                target_value=0.5,
                warning_threshold=None,
                critical_threshold=None,
                valid_from=date(2026, 1, 1),
                valid_until=None,
            )

    def test_global_scope_must_not_carry_a_scope_id(self):
        with pytest.raises(ValueError):
            MetricTarget(
                metric_name="successful_procurement_rate",
                scope_type=TargetScopeType.GLOBAL,
                scope_id="some-id",
                target_value=0.5,
                warning_threshold=None,
                critical_threshold=None,
                valid_from=date(2026, 1, 1),
                valid_until=None,
            )

    def test_non_global_scope_requires_a_scope_id(self):
        with pytest.raises(ValueError):
            MetricTarget(
                metric_name="recurring_coverage_rate",
                scope_type=TargetScopeType.SUBCATEGORY,
                scope_id=None,
                target_value=0.8,
                warning_threshold=None,
                critical_threshold=None,
                valid_from=date(2026, 1, 1),
                valid_until=None,
            )

    def test_a_valid_target_constructs_cleanly(self):
        target = MetricTarget(
            metric_name="successful_procurement_rate",
            scope_type=TargetScopeType.GLOBAL,
            scope_id=None,
            target_value=0.5,
            warning_threshold=0.4,
            critical_threshold=0.3,
            valid_from=date(2026, 1, 1),
            valid_until=None,
        )
        assert target.target_value == 0.5

    def test_example_targets_are_themselves_valid(self):
        assert len(EXAMPLE_TARGETS) >= 2
