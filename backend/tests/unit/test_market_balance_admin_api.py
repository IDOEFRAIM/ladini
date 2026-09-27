"""Phase E — internal Market Balance API: parameter validation, authorization, and the shim that
lets it reuse the buyer side's zone-hierarchy `resolve_filters` without duplicating that SQL.
Real-SQL behaviour is covered by tests/schema/test_market_balance_admin_api_pg.py (CI).

NOTE: `TestAuthorization` imports `ladini.api.main.app`, exactly like its buyer/producer-side
siblings already do — self-skips identically in a local shell missing a valid REDIS_URL (a
pre-existing, unrelated environment gap), and runs for real in CI, which does have one configured."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


class TestParams:
    def test_all_filters_optional_and_ids_validated(self):
        from ladini.services.analytics.market_balance_admin_api import (
            ApiError,
            parse_params,
        )

        p = parse_params({})
        assert p.zone_scope is None and p.category_id is None and p.sub_category_id is None and p.canonical_unit is None
        z = str(uuid.uuid4())
        assert parse_params({"zone_scope": z, "canonical_unit": "KG"}) == parse_params({"zone_scope": z, "canonical_unit": "KG"})
        assert parse_params({"zone_scope": z}).zone_scope == z
        with pytest.raises(ApiError):
            parse_params({"zone_scope": "not-a-uuid"})

    def test_cache_key_reflects_every_filter(self):
        from ladini.services.analytics.market_balance_admin_api import parse_params

        z, c, s = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        key = parse_params({"zone_scope": z, "category_id": c, "sub_category_id": s, "canonical_unit": "TETE"}).cache_key
        assert key == {"zone": z, "category": c, "sub_category": s, "unit": "TETE"}


class TestZoneIdShim:
    def test_shim_exposes_zone_scope_under_the_name_buyer_resolve_filters_expects(self):
        from ladini.services.analytics.market_balance_admin_api import (
            Params,
            _ZoneIdShim,
        )

        z, c, s = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        p = Params(zone_scope=z, category_id=c, sub_category_id=s, canonical_unit="KG")
        shim = _ZoneIdShim(p)
        assert (shim.zone_id, shim.category_id, shim.sub_category_id) == (z, c, s)


class TestTimeseriesWindowValidation:
    """`resolve_window` is deliberately pure and called BEFORE any database session is opened
    (see the router) — a reversed/oversized window must fail with ApiError(400) without ever
    touching `MarketBalanceService`/a session, never surface as a 500 from a session that was
    never needed (the exact bug this test locks: CI caught it as a 500 on the first push)."""

    def test_reversed_window_is_rejected_without_touching_a_session(self):
        from datetime import date

        from ladini.services.analytics.market_balance_admin_api import (
            ApiError,
            resolve_window,
        )

        with pytest.raises(ApiError) as e:
            resolve_window(date(2026, 9, 10), date(2026, 9, 1))
        assert e.value.status == 400

    def test_oversized_window_is_rejected_without_touching_a_session(self):
        from datetime import date

        from ladini.services.analytics.market_balance_admin_api import (
            ApiError,
            resolve_window,
        )

        with pytest.raises(ApiError) as e:
            resolve_window(date(2020, 1, 1), date(2026, 9, 1))
        assert e.value.status == 400

    def test_missing_bounds_default_to_the_standard_window(self):
        from ladini.services.analytics.market_balance_admin_api import (
            DEFAULT_WINDOW_DAYS,
            resolve_window,
        )

        s, e = resolve_window(None, None)
        assert (e - s).days + 1 == DEFAULT_WINDOW_DAYS


class TestAuthorization:
    def _client(self, monkeypatch, token):
        from ladini.api.main import app
        from ladini.core.settings import settings

        monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", token, raising=False)
        return TestClient(app, raise_server_exceptions=False)

    PATHS = ["/internal/analytics/market-balance/overview", "/internal/analytics/market-balance/current",
             "/internal/analytics/market-balance/demand-gaps", "/internal/analytics/market-balance/excess-supply",
             "/internal/analytics/market-balance/timeseries", "/internal/analytics/market-balance/filters",
             "/internal/analytics/market-balance/health"]

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
        assert c.get("/internal/analytics/market-balance/overview?zone_scope=not-a-uuid", headers=h).status_code == 400
        assert c.get("/internal/analytics/market-balance/timeseries?from=2026-09-10&to=2026-09-01", headers=h).status_code == 400

    def test_internal_errors_never_leak(self, monkeypatch):
        import ladini.api.routes.analytics_admin_market_balance as route

        monkeypatch.setenv("ANALYTICS_CACHE_TTL_SECONDS", "0")

        def boom():
            raise RuntimeError("postgres://user:pass@db/secret")

        monkeypatch.setattr(route, "get_sessionmaker", boom)
        c = self._client(monkeypatch, "secret")
        r = c.get("/internal/analytics/market-balance/overview", headers={"X-Internal-Token": "secret"})
        assert r.status_code == 500 and "postgres" not in r.text and "secret" not in r.text.replace("Erreur", "")


class TestHealthStatusRules:
    def test_health_status_rules(self, monkeypatch):
        import ladini.services.analytics.market_balance_admin_api as api
        from tests.conftest import run

        for stale, issues, expected in ((True, [], "Stale"), (False, [], "Healthy"), (False, ["x"], "Warning")):

            class FakeService:
                def __init__(self, session):
                    pass

                async def freshness(self, *, now=None, _stale=stale):
                    return {"stale": _stale, "last_refresh": None}

            async def fake_checks(session, *, now=None, _i=issues):
                from types import SimpleNamespace
                return [SimpleNamespace(check="duplicate_grain", table="t", severity="ERROR", count=1, detail="d") for _ in _i]

            monkeypatch.setattr(api, "MarketBalanceService", FakeService)
            monkeypatch.setattr(api, "run_market_balance_quality_checks", fake_checks)
            assert run(api.health(object()))["status"] == expected
