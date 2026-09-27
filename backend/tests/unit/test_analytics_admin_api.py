"""Phase E — internal analytics API: parameter validation, authorization, cache fail-open, and
response shaping (PARTIAL/UNAVAILABLE/mixed units), without a database. Real-SQL behaviour is covered
by tests/schema/test_analytics_admin_api_pg.py (CI)."""
from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.conftest import run

TODAY = date(2026, 9, 27)


def _p(**q):
    from ladini.services.analytics.admin_api import parse_params

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
        from ladini.services.analytics.admin_api import ApiError

        with pytest.raises(ApiError) as e:
            _p(**{"from": bad})
        assert e.value.status == 400

    def test_reversed_and_oversized_windows_are_rejected(self):
        from ladini.services.analytics.admin_api import ApiError

        with pytest.raises(ApiError):
            _p(**{"from": "2026-09-10", "to": "2026-09-01"})
        with pytest.raises(ApiError):
            _p(**{"from": "2020-01-01", "to": "2026-09-01"})

    def test_ids_and_journey_are_validated(self):
        from ladini.services.analytics.admin_api import ApiError

        for q in ({"zone_id": "nope"}, {"category_id": "1"}, {"sub_category_id": "x"}, {"journey": "PRODUCER"}):
            with pytest.raises(ApiError):
                _p(**q)
        z = str(uuid.uuid4())
        assert _p(zone_id=z, journey="recurring").journey == "RECURRING" and _p(zone_id=z).zone_id == z

    def test_paging_bounds(self):
        from ladini.services.analytics.admin_api import ApiError, parse_paging

        assert parse_paging({}) == (100, 0)
        for q in ({"limit": "0"}, {"limit": "9999"}, {"offset": "-1"}, {"limit": "x"}):
            with pytest.raises(ApiError):
                parse_paging(q)


class TestCache:
    def test_failing_cache_falls_back_to_computing(self):
        from ladini.services.analytics.cache import cached

        class Broken:
            async def get(self, k):
                raise ConnectionError("down")

            async def set(self, *a, **k):
                raise ConnectionError("down")

        calls = []

        async def produce():
            calls.append(1)
            return {"x": 1}

        out = run(cached("overview", {"a": 1}, produce, ttl=60, client=Broken()))
        assert out["x"] == 1 and out["cache"] == "MISS" and calls == [1]

    def test_hit_skips_the_producer_and_ttl_zero_disables(self):
        import json

        from ladini.services.analytics.cache import cached

        class Store:
            def __init__(self):
                self.d = {}

            async def get(self, k):
                return self.d.get(k)

            async def set(self, k, v, ex=None):
                self.d[k] = v

        store, calls = Store(), []

        async def produce():
            calls.append(1)
            return {"n": len(calls)}

        first = run(cached("e", {"q": 1}, produce, ttl=60, client=store))
        second = run(cached("e", {"q": 1}, produce, ttl=60, client=store))
        assert (first["cache"], second["cache"], calls) == ("MISS", "HIT", [1])
        assert json.loads(next(iter(store.d.values())))["n"] == 1
        run(cached("e", {"q": 1}, produce, ttl=0, client=store))
        assert len(calls) == 2  # ttl 0 bypasses the cache entirely

    def test_different_filters_never_share_a_key(self):
        from ladini.services.analytics.cache import make_key

        assert make_key("overview", {"zone": "a"}) != make_key("overview", {"zone": "b"})


class TestAuthorization:
    def _client(self, monkeypatch, token):
        from ladini.api.main import app
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", token, raising=False)
        return TestClient(app, raise_server_exceptions=False)

    PATHS = ["/internal/analytics/buyers/overview", "/internal/analytics/buyers/direct", "/internal/analytics/buyers/tenders",
             "/internal/analytics/buyers/recurring", "/internal/analytics/buyers/unmatched-demand", "/internal/analytics/buyers/filters",
             "/internal/analytics/buyers/health", "/internal/analytics/buyers/compare?metric=needs_created",
             "/internal/analytics/buyers/metrics/needs_created/timeseries", "/internal/analytics/buyers/metrics/needs_created/breakdown?dimension=zone_id"]

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
        assert c.get("/internal/analytics/buyers/overview?from=garbage", headers=h).status_code == 400
        assert c.get("/internal/analytics/buyers/unmatched-demand?limit=0", headers=h).status_code == 400

    def test_internal_errors_never_leak(self, monkeypatch):
        import ladini.api.routes.analytics_admin as route

        monkeypatch.setenv("ANALYTICS_CACHE_TTL_SECONDS", "0")

        def boom():
            raise RuntimeError("postgres://user:pass@db/secret")

        monkeypatch.setattr(route, "get_sessionmaker", boom)
        c = self._client(monkeypatch, "secret")
        r = c.get("/internal/analytics/buyers/overview", headers={"X-Internal-Token": "secret"})
        assert r.status_code == 500 and "postgres" not in r.text and "secret" not in r.text.replace("Erreur", "")


class TestPayloadShaping:
    def _res(self, **over):
        from ladini.domain.analytics.metric_layer import DataStatus, MetricResult

        base = dict(metric_name="recurring_coverage_rate", value=0.69, numerator=4310.0, denominator=6240.0, unit="ratio",
                    status=DataStatus.OK, period={"start": "a", "end": "b"}, previous_value=0.61, delta=0.08)
        base.update(over)
        return MetricResult(**base)

    def test_rates_carry_percentage_point_deltas_and_reliability(self):
        from ladini.services.analytics.admin_api import metric_payload

        d = metric_payload(self._res())
        assert d["delta_kind"] == "percentage_points" and d["delta_points"] == pytest.approx(8.0) and d["delta_pct"] is None
        assert d["reliability"] == "RELIABLE" and d["value"] == 0.69

    def test_non_ratio_metrics_carry_a_relative_delta(self):
        from ladini.services.analytics.admin_api import metric_payload

        d = metric_payload(self._res(metric_name="potential_gmv", unit="FCFA", value=1200.0, previous_value=1000.0, delta=200.0))
        assert d["delta_kind"] == "relative" and d["delta_pct"] == pytest.approx(0.2) and d["reliability"] == "PARTIAL"

    def test_unavailable_metric_is_null_with_a_reason_not_zero(self):
        from ladini.domain.analytics.metric_layer import UNAVAILABLE, unavailable_result
        from ladini.services.analytics.admin_api import metric_payload

        d = metric_payload(unavailable_result("recurring_modification_rate", {"start": "a", "end": "b"}))
        assert d["value"] is None and d["reliability"] == "UNAVAILABLE" and d["status"] == "UNAVAILABLE"
        assert UNAVAILABLE["recurring_modification_rate"] in d["notes"]

    def test_unsupported_filter_becomes_unavailable_not_zero(self):
        from ladini.services.analytics.admin_api import safe_metric

        class Svc:
            async def get_metric(self, *a, **k):
                raise ValueError("Filter 'category_id' is not a dimension of analytics.buyer_daily_metrics")

        p = _p()
        d = run(safe_metric(Svc(), "needs_created", p, {"category_id": str(uuid.uuid4())}))
        assert d["status"] == "UNAVAILABLE" and d["value"] is None and "Not available" in d["notes"][0]

    def test_mixed_units_keep_the_per_unit_breakdown_and_no_global_number(self):
        from ladini.domain.analytics.metric_layer import DataStatus
        from ladini.services.analytics.admin_api import metric_payload

        res = self._res(metric_name="recurring_requested_quantity", value=None, numerator=None, denominator=None, unit=None,
                        status=DataStatus.MIXED_UNITS, previous_value=None, delta=None,
                        breakdown=[{"canonical_unit": "KG", "numerator": 4200.0}, {"canonical_unit": "L", "numerator": 850.0}])
        d = metric_payload(res)
        assert d["status"] == "MIXED_UNITS" and d["numerator"] is None and [b["canonical_unit"] for b in d["breakdown"]] == ["KG", "L"]

    def test_series_validation(self):
        from ladini.services.analytics.admin_api import ApiError, _series_binding

        p = _p()
        with pytest.raises(ApiError) as e:
            _series_binding("recurring_modification_rate", p)
        assert e.value.status == 400
        with pytest.raises(ApiError) as e:
            _series_binding("not_a_metric", p)
        assert e.value.status == 404
        with pytest.raises(ApiError):
            _series_binding("tender_response_rate", _p(journey="DIRECT"))
        _series_binding("needs_created", _p(journey="DIRECT"))  # GLOBAL metrics are fine under any journey
        _series_binding("active_buyers", p)

    def test_health_status_rules(self, monkeypatch):
        import ladini.services.analytics.admin_api as api

        async def fresh(stale):
            return {"stale": stale, "last_refresh": None}

        async def dq(issues):
            return issues

        for stale, issues, expected in ((True, [], "Stale"), (False, [], "Healthy"), (False, ["x"], "Warning")):
            async def fake_fresh(session, now=None, _s=stale):
                return {"stale": _s, "last_refresh": None}

            async def fake_dq(session, s, e, now=None, _i=issues):
                return [SimpleNamespace(check="duplicate_grain", table="t", severity="ERROR", count=1, detail="d") for _ in _i]

            monkeypatch.setattr(api, "freshness", fake_fresh)
            monkeypatch.setattr(api, "run_data_quality_checks", fake_dq)
            assert run(api.health(object()))["status"] == expected

    def test_stale_refresh_from_an_empty_table_alone_is_not_a_warning(self, monkeypatch):
        import ladini.services.analytics.admin_api as api

        async def fake_fresh(session, now=None):
            return {"stale": False, "last_refresh": "x"}

        async def fake_dq(session, s, e, now=None):
            return [SimpleNamespace(check="stale_refresh", table="analytics.tender_daily_metrics", severity="WARNING", count=1, detail="Never refreshed.")]

        monkeypatch.setattr(api, "freshness", fake_fresh)
        monkeypatch.setattr(api, "run_data_quality_checks", fake_dq)
        assert run(api.health(object()))["status"] == "Healthy"
