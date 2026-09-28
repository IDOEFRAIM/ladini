"""Market Balance (Phase E) — the admin API payload builders against a real PostgreSQL (CI):
zone hierarchy (reused via the `_ZoneIdShim`, proving it actually works against real recursive
zone SQL, not just in isolation), labels, grouped demand-gaps/excess-supply, overview totals per
unit, and health. Rows are inserted directly into `market_balance_daily_snapshot` — the write path
(`recompute_today`) has its own dedicated tests in test_market_balance_refresh_pg.py; this file
is about the READ layer only. Mirrors test_producer_admin_api_pg.py's structure."""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.services.analytics.market_balance_admin_api as api

_DAYS = iter(range(1, 500))


def _fresh_day() -> date:
    # Isolated base, distinct from every other tests/schema/*.py file's own counter (see
    # test_producer_admin_api_pg.py's identical comment for the collision class this avoids) —
    # 1995-01-01 + <500 days ends ~1996-05, safely below every known base in this directory.
    return date(1995, 1, 1) + timedelta(days=next(_DAYS))


def _run(dsn, fn):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _sql(dsn, statement, params=()):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(statement, params)
        out = cur.fetchall() if cur.description else None
    conn.close()
    return out


class SimpleNamespace_:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def world(pg_dsn):
    """Zone hierarchy parent > child + a category/sub-category — same shape as the buyer/producer
    `world` fixtures, since Market Balance reuses the exact same zone-hierarchy `resolve_filters`."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        region = insert(cur, "governance.climatic_regions", name=uniq("region"))
        child_name = uniq("child")
        child = insert(cur, "governance.zones", name=child_name, code=uniq("C"), climatic_region_id=region, parent_id=g.zone)
        cur.execute("select name from governance.zones where id = %s", (g.zone,))
        parent_name = cur.fetchone()[0]
        cur.execute("select category_id from governance.sub_categories where id = %s", (g.sub_category,))
        category = cur.fetchone()[0]
    conn.close()
    return SimpleNamespace_(dsn=pg_dsn, g=g, parent=str(g.zone), child=str(child), category=str(category),
                            sub1=str(g.sub_category), parent_name=parent_name, child_name=child_name)


def _snapshot(w, day, zone, *, unit="KG", demand=100, supply=80, scope="RECURRING", sub=None):
    family = "COUNT" if unit == "TETE" else "MASS"
    coverable = min(demand, supply)
    gap = max(demand - supply, 0)
    excess = max(supply - demand, 0)
    _sql(w.dsn,
         "insert into analytics.market_balance_daily_snapshot (snapshot_day, zone_scope, category_id, sub_category_id, "
         "canonical_unit, measurement_family, demand_scope, open_demand_quantity, available_supply_quantity, "
         "potential_coverable_quantity, demand_gap_quantity, excess_supply_quantity) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
         (day, zone, w.category, sub or w.sub1, unit, family, scope, demand, supply, coverable, gap, excess))


def _params(**kw):
    return api.Params(**kw)


def test_current_includes_zone_descendants_and_attaches_labels(world):
    w = world
    d = _fresh_day()
    _snapshot(w, d, w.child, demand=100, supply=60)
    _snapshot(w, d, w.parent, demand=50, supply=50)
    out = _run(w.dsn, lambda s: api.current(s, _params(zone_scope=w.parent)))
    assert out["status"] == "OK"
    rows = {r["zone"]: r for r in out["rows"]}
    assert set(rows) == {w.parent, w.child}
    assert rows[w.child]["zone_label"] == w.child_name
    assert rows[w.parent]["zone_label"] == w.parent_name
    child_only = _run(w.dsn, lambda s: api.current(s, _params(zone_scope=w.child)))
    assert {r["zone"] for r in child_only["rows"]} == {w.child}


def test_overview_totals_per_unit_never_mixes_incompatible_units(world):
    w = world
    d = _fresh_day()
    _snapshot(w, d, w.parent, unit="KG", demand=1000, supply=800)
    _snapshot(w, d, w.parent, unit="TETE", demand=10, supply=15, sub=w.sub1)
    out = _run(w.dsn, lambda s: api.overview(s, _params(zone_scope=w.parent)))
    by_unit = {b["canonical_unit"]: b for b in out["by_unit"]}
    assert by_unit["KG"]["demand_gap_quantity"] == 200.0
    assert by_unit["TETE"]["excess_supply_quantity"] == 5.0
    assert by_unit["KG"]["potential_coverage_rate"] == pytest.approx(0.8)


def test_demand_gaps_are_grouped_and_labelled(world):
    w = world
    d = _fresh_day()
    _snapshot(w, d, w.parent, unit="KG", demand=500, supply=100)
    out = _run(w.dsn, lambda s: api.demand_gaps(s, _params(zone_scope=w.parent), limit=50))
    assert out["status"] == "OK"
    group = out["groups"][0]
    assert group["canonical_unit"] == "KG"
    assert group["rows"][0]["demand_gap_quantity"] == 400.0
    assert group["rows"][0]["subcategory_label"]


def test_excess_supply_is_grouped_and_labelled(world):
    w = world
    d = _fresh_day()
    _snapshot(w, d, w.parent, unit="KG", demand=10, supply=900)
    out = _run(w.dsn, lambda s: api.excess_supply(s, _params(zone_scope=w.parent), limit=50))
    assert out["groups"][0]["rows"][0]["excess_supply_quantity"] == 890.0


def test_timeseries_only_contains_real_snapshot_days(world):
    w = world
    d1 = _fresh_day()
    d2 = d1 + timedelta(days=1)
    _sql(w.dsn, "insert into analytics.market_balance_daily_snapshot (snapshot_day, zone_scope, category_id, sub_category_id, "
                "canonical_unit, measurement_family, demand_scope, open_demand_quantity, available_supply_quantity, "
                "potential_coverable_quantity, demand_gap_quantity, excess_supply_quantity) values (%s,%s,%s,%s,'KG','MASS','RECURRING',100,60,60,40,0)",
         (d1, w.parent, w.category, w.sub1))
    _sql(w.dsn, "insert into analytics.market_balance_daily_snapshot (snapshot_day, zone_scope, category_id, sub_category_id, "
                "canonical_unit, measurement_family, demand_scope, open_demand_quantity, available_supply_quantity, "
                "potential_coverable_quantity, demand_gap_quantity, excess_supply_quantity) values (%s,%s,%s,%s,'KG','MASS','RECURRING',50,50,50,0,0)",
         (d2, w.parent, w.category, w.sub1))
    out = _run(w.dsn, lambda s: api.timeseries(s, _params(zone_scope=w.parent), start=d1, end=d2))
    assert [p["demand_gap_quantity"] for p in out["points"]] == [40.0, 0.0]


def test_health_status_rules_against_real_freshness(world):
    w = world
    d = _fresh_day()
    _snapshot(w, d, w.parent)
    fresh = _run(w.dsn, lambda s: api.health(s))
    assert fresh["status"] in ("Healthy", "Warning")
    later = datetime.now(timezone.utc) + timedelta(days=3)
    stale = _run(w.dsn, lambda s: api.health(s, now=later))
    assert stale["status"] == "Stale"


def test_current_and_overview_latency_over_a_wide_zone(world):
    w = world
    d = _fresh_day()
    _sql(w.dsn,
         "insert into analytics.market_balance_daily_snapshot (snapshot_day, zone_scope, category_id, sub_category_id, "
         "canonical_unit, measurement_family, demand_scope, open_demand_quantity, available_supply_quantity, "
         "potential_coverable_quantity, demand_gap_quantity, excess_supply_quantity) "
         "select %s, %s::uuid, %s::uuid, gen_random_uuid(), 'KG', 'MASS', 'RECURRING', 100, 60, 60, 40, 0 from generate_series(1, 200)",
         (d, w.parent, w.category))

    async def fn(s):
        t0 = time.perf_counter()
        await api.current(s, _params(zone_scope=w.parent))
        await api.overview(s, _params(zone_scope=w.parent))
        return time.perf_counter() - t0

    assert _run(w.dsn, fn) < 5.0
