"""Phase D — internal producer-analytics API: parameter validation, authorization, cache
fail-open, and response shaping (PARTIAL/UNAVAILABLE/mixed units), without a database. Real-SQL
behaviour is covered by tests/schema/test_producer_admin_api_pg.py (CI). Mirrors
tests/unit/test_analytics_admin_api.py (buyer, Phase E) — same posture, minus the `journey`
dimension the producer side doesn't have.

NOTE: `TestAuthorization` imports `ladini.api.main.app`, exactly like its buyer-side sibling
already does — both self-skip identically in a local shell missing a valid REDIS_URL (a
pre-existing, unrelated environment gap: `twilio_webhook.py` builds a Redis client at import
time), and both run for real in CI, which does have one configured."""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import run

TODAY = date(2026, 9, 27)


def _p(**q):
    from ladini.services.analytics.producer_admin_api import parse_params

    return parse_params(q, today=TODAY)


class TestParams:
    def test_defaults_to_the_last_30_utc_days_inclusive(self):
        p = _p()
        assert (p.start, p.end) == (date(2026, 8, 29), TODAY) and (p.end - p.start).days + 1 == 30

    def test_explicit_inclusive_bounds(self):
        p = _p(**{"from": "2026-09-01", "to": "2026-09-01"})
        assert p.start == p.end == date(2026, 9, 1)

    @pytest.mark.parametrize("bad", ["2026-09-01T00:00:00Z", "2026-9-1", "01/09/2026", "abc"])
    def test_time_components_and_bad_formats_are_rejected(self, bad):
        from ladini.services.analytics.producer_admin_api import ApiError

        with pytest.raises(ApiError) as e:
            _p(**{"from": bad})
        assert e.value.status == 400

    def test_reversed_and_oversized_windows_are_rejected(self):
        from ladini.services.analytics.producer_admin_api import ApiError

        with pytest.raises(ApiError):
            _p(**{"from": "2026-09-10", "to": "2026-09-01"})
        with pytest.raises(ApiError):
            _p(**{"from": "2020-01-01", "to": "2026-09-01"})

    def test_ids_are_validated_and_there_is_no_journey_field(self):
        from ladini.services.analytics.producer_admin_api import ApiError

        for q in ({"zone_id": "nope"}, {"category_id": "1"}, {"sub_category_id": "x"}):
            with pytest.raises(ApiError):
                _p(**q)
        z = str(uuid.uuid4())
        assert _p(zone_id=z).zone_id == z
        assert not hasattr(_p(), "journey")

    def test_paging_bounds(self):
        from ladini.services.analytics.producer_admin_api import ApiError, parse_paging

        assert parse_paging({}) == (100, 0)
        for q in ({"limit": "0"}, {"limit": "9999"}, {"offset": "-1"}, {"limit": "x"}):
            with pytest.raises(ApiError):
                parse_paging(q)


class TestAuthorization:
    def _client(self, monkeypatch, token):
        from ladini.api.main import app
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", token, raising=False)
        return TestClient(app, raise_server_exceptions=False)

    PATHS = ["/internal/analytics/producers/overview", "/internal/analytics/producers/supply",
             "/internal/analytics/producers/fulfillment", "/internal/analytics/producers/gmv",
             "/internal/analytics/producers/retention", "/internal/analytics/producers/filters",
             "/internal/analytics/producers/health", "/internal/analytics/producers/compare?metric=active_producers",
             "/internal/analytics/producers/metrics/active_producers/timeseries",
             "/internal/analytics/producers/metrics/producer_delivered_gmv/breakdown?dimension=zone_id"]

    @pytest.mark.parametrize("path", PATHS)
    def test_no_or_wrong_token_is_401_and_unconfigured_is_503(self, monkeypatch, path):
        c = self._client(monkeypatch, "secret")
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"X-Internal-Token": "wrong"}).status_code == 401
        c = self._client(monkeypatch, "")
        assert c.get(path, headers={"X-Internal-Token": "secret"}).status_code == 503

    def test_valid_token_reaches_the_handler_and_bad_params_are_400(self, monkeypatch):
        monkeypatch.setenv("ANALYTICS_CACHE_TTL_SECONDS", "0")
        c = self._client(monkeypatch, "secret")
        h = {"X-Internal-Token": "secret"}
        assert c.get("/internal/analytics/producers/overview?from=garbage", headers=h).status_code == 400
        assert c.get("/internal/analytics/producers/metrics/producer_delivered_gmv/breakdown?limit=0", headers=h).status_code == 400

    def test_internal_errors_never_leak(self, monkeypatch):
        import ladini.api.routes.analytics_admin_producers as route

        monkeypatch.setenv("ANALYTICS_CACHE_TTL_SECONDS", "0")

        def boom():
            raise RuntimeError("postgres://user:pass@db/secret")

        monkeypatch.setattr(route, "get_sessionmaker", boom)
        c = self._client(monkeypatch, "secret")
        r = c.get("/internal/analytics/producers/overview", headers={"X-Internal-Token": "secret"})
        assert r.status_code == 500 and "postgres" not in r.text and "secret" not in r.text.replace("Erreur", "")


class TestPayloadShaping:
    def _res(self, **over):
        from ladini.domain.analytics.metric_layer import DataStatus, MetricResult

        base = dict(metric_name="producer_order_fulfillment_rate", value=0.72, numerator=180.0, denominator=250.0, unit="ratio",
                    status=DataStatus.OK, period={"start": "a", "end": "b"}, previous_value=0.65, delta=0.07)
        base.update(over)
        return MetricResult(**base)

    def test_rates_carry_percentage_point_deltas_and_reliability(self):
        from ladini.services.analytics.producer_admin_api import metric_payload

        d = metric_payload(self._res())
        assert d["delta_kind"] == "percentage_points" and d["delta_points"] == pytest.approx(7.0) and d["delta_pct"] is None
        assert d["reliability"] == "RELIABLE" and d["value"] == 0.72

    def test_non_ratio_metrics_carry_a_relative_delta(self):
        from ladini.services.analytics.producer_admin_api import metric_payload

        d = metric_payload(self._res(metric_name="producer_delivered_gmv", unit="FCFA", value=1200.0, previous_value=1000.0, delta=200.0))
        assert d["delta_kind"] == "relative" and d["delta_pct"] == pytest.approx(0.2) and d["reliability"] == "RELIABLE"

    def test_partial_metrics_are_flagged(self):
        from ladini.services.analytics.producer_admin_api import metric_payload

        d = metric_payload(self._res(metric_name="producer_quantity_fulfillment_rate"))
        assert d["reliability"] == "PARTIAL"
        d2 = metric_payload(self._res(metric_name="repeat_producer_rate"))
        assert d2["reliability"] == "PARTIAL"
        d3 = metric_payload(self._res(metric_name="time_to_first_sale", unit="days"))
        assert d3["reliability"] == "PARTIAL"

    def test_unavailable_metric_is_null_with_a_reason_not_zero(self):
        from ladini.domain.analytics.producer_metric_layer import UNAVAILABLE_PRODUCER
        from ladini.services.analytics.producer_admin_api import metric_payload
        from ladini.services.analytics.producer_analytics_service import (
            unavailable_result_producer,
        )

        d = metric_payload(unavailable_result_producer("producer_sell_through_rate", {"start": "a", "end": "b"}))
        assert d["value"] is None and d["reliability"] == "UNAVAILABLE" and d["status"] == "UNAVAILABLE"
        assert UNAVAILABLE_PRODUCER["producer_sell_through_rate"] in d["notes"]

    def test_unsupported_filter_becomes_unavailable_not_zero(self):
        from ladini.services.analytics.producer_admin_api import safe_metric

        class Svc:
            async def get_metric(self, *a, **k):
                raise ValueError("Filter 'zone_id' is not a dimension of analytics.producer_quantity_daily_metrics")

        p = _p()
        d = run(safe_metric(Svc(), "producer_quantity_fulfillment_rate", p, {"zone_id": [str(uuid.uuid4())]}))
        assert d["status"] == "UNAVAILABLE" and d["value"] is None and "Not available" in d["notes"][0]

    def test_mixed_units_keep_the_per_unit_breakdown_and_no_global_number(self):
        from ladini.domain.analytics.metric_layer import DataStatus
        from ladini.services.analytics.producer_admin_api import metric_payload

        res = self._res(metric_name="producer_quantity_fulfillment_rate", value=None, numerator=None, denominator=None, unit=None,
                        status=DataStatus.MIXED_UNITS, previous_value=None, delta=None,
                        breakdown=[{"canonical_unit": "KG", "numerator": 4200.0}, {"canonical_unit": "TETE", "numerator": 12.0}])
        d = metric_payload(res)
        assert d["status"] == "MIXED_UNITS" and d["numerator"] is None and [b["canonical_unit"] for b in d["breakdown"]] == ["KG", "TETE"]

    def test_series_validation(self):
        from ladini.services.analytics.producer_admin_api import (
            ApiError,
            _series_binding,
        )

        p = _p()
        with pytest.raises(ApiError) as e:
            _series_binding("producer_sell_through_rate", p)
        assert e.value.status == 400
        with pytest.raises(ApiError) as e:
            _series_binding("not_a_metric", p)
        assert e.value.status == 404
        _series_binding("active_producers", p)
        _series_binding("producer_delivered_gmv", p)

    def test_where_ignores_zone_scope_a_resolve_filters_artifact_not_a_real_dimension(self):
        """`admin_api.resolve_filters` (buyer, reused as-is for producers — see `producer_admin_api.resolve_filters`)
        always injects a `zone_scope` key alongside the expanded `zone_id` list, for target resolution only. The
        service's own `_where` must ignore it like the buyer's own `_where` already does, or every producer metric
        request carrying a zone filter would blow up with 'zone_scope is not a dimension' (a real bug this test
        would have caught before it reached CI)."""
        from ladini.domain.analytics.producer_metric_layer import TABLE_PRODUCER
        from ladini.services.analytics.producer_analytics_service import (
            ProducerAnalyticsService,
        )

        z = str(uuid.uuid4())
        where, params = ProducerAnalyticsService._where(TABLE_PRODUCER, {"zone_id": [z], "zone_scope": z})
        assert "zone_scope" not in where and "zone_scope" not in params
        assert "zone_id" in where

    def test_breakdown_dimension_must_match_the_bindings_own_table(self):
        from ladini.domain.analytics.producer_metric_layer import (
            BINDINGS_PRODUCER,
            TABLE_DIMENSIONS_PRODUCER,
        )

        gmv = BINDINGS_PRODUCER["producer_delivered_gmv"]
        assert TABLE_DIMENSIONS_PRODUCER[gmv.table] == ("zone_id",)
        qty = BINDINGS_PRODUCER["producer_quantity_fulfillment_rate"]
        assert TABLE_DIMENSIONS_PRODUCER[qty.table] == ("canonical_unit",)

    def test_health_status_rules(self, monkeypatch):
        import ladini.services.analytics.producer_admin_api as api

        for stale, issues, expected in ((True, [], "Stale"), (False, [], "Healthy"), (False, ["x"], "Warning")):

            async def fake_fresh(session, now=None, _s=stale):
                return {"stale": _s, "last_refresh": None}

            async def fake_dq(session, s, e, now=None, _i=issues):
                return [SimpleNamespace(check="duplicate_grain", table="t", severity="ERROR", count=1, detail="d") for _ in _i]

            monkeypatch.setattr(api, "freshness", fake_fresh)
            monkeypatch.setattr(api, "run_producer_metric_quality_checks", fake_dq)
            assert run(api.health(object()))["status"] == expected

    def test_stale_refresh_from_an_empty_table_alone_is_not_a_warning(self, monkeypatch):
        import ladini.services.analytics.producer_admin_api as api

        async def fake_fresh(session, now=None):
            return {"stale": False, "last_refresh": "x"}

        async def fake_dq(session, s, e, now=None):
            return [SimpleNamespace(check="stale_refresh", table="analytics.producer_supply_daily_snapshot", severity="WARNING", count=1, detail="Never refreshed.")]

        monkeypatch.setattr(api, "freshness", fake_fresh)
        monkeypatch.setattr(api, "run_producer_metric_quality_checks", fake_dq)
        assert run(api.health(object()))["status"] == "Healthy"
