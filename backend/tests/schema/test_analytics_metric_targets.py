"""Real-PostgreSQL tests for `analytics.metric_targets` (mission Phase C,
section 13, items F/G). Self-skips locally without SCHEMA_TEST_DSN."""
from __future__ import annotations

import uuid
from datetime import date

import psycopg2
import pytest
from factories import insert


class TestMetricTargetsScopeValidation:
    def test_global_scope_with_a_scope_id_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.metric_targets",
                metric_name="successful_procurement_rate", scope_type="GLOBAL",
                scope_id=uuid.uuid4(), target_value=0.5, valid_from=date(2026, 1, 1),
            )

    def test_subcategory_scope_without_a_scope_id_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.metric_targets",
                metric_name="recurring_coverage_rate", scope_type="SUBCATEGORY",
                scope_id=None, target_value=0.8, valid_from=date(2026, 1, 1),
            )

    def test_global_scope_without_a_scope_id_is_accepted(self, db):
        cur = db.cursor()
        target_id = insert(
            cur, "analytics.metric_targets",
            metric_name="successful_procurement_rate", scope_type="GLOBAL",
            scope_id=None, target_value=0.5, valid_from=date(2026, 1, 1),
        )
        assert target_id is not None

    def test_category_scope_with_a_scope_id_is_accepted(self, db):
        cur = db.cursor()
        target_id = insert(
            cur, "analytics.metric_targets",
            metric_name="recurring_coverage_rate", scope_type="CATEGORY",
            scope_id=uuid.uuid4(), target_value=0.75, valid_from=date(2026, 1, 1),
        )
        assert target_id is not None

    def test_invalid_scope_type_is_rejected(self, db):
        cur = db.cursor()
        with pytest.raises(psycopg2.errors.CheckViolation):
            insert(
                cur, "analytics.metric_targets",
                metric_name="successful_procurement_rate", scope_type="COUNTRY",
                scope_id=None, target_value=0.5, valid_from=date(2026, 1, 1),
            )
