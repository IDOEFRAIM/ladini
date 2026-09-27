"""Phase D — the producer analytics API payload builders against a real PostgreSQL (CI):
zone hierarchy, mixed-unit quantity breakdown, the live available_supply gauge, unsupported
filters becoming UNAVAILABLE (never a silent 0/zero), breakdowns, series, freshness/health and
a latency guard. Mirrors tests/schema/test_analytics_admin_api_pg.py (buyer, Phase E).

Isolated day base (2005-01-01, small counter): every other tests/schema/*.py file's own
`_fresh_day()` counter starts at 2010 or later (see test_producer_metric_layer_pg.py's own
comment on the day-collision bug class this avoids) — 2005 + a few hundred days stays far
below all of them, on a shared-DB-per-session test suite where two files picking the same
calendar day would cross-contaminate each other's day-scoped aggregates."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.services.analytics.producer_admin_api as api

_DAYS = iter(range(1, 500))


def _day() -> date:
    return date(2005, 1, 1) + timedelta(days=next(_DAYS))


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


def _params(day, end=None, **kw):
    return api.Params(day, end or day, **kw)


class SimpleNamespace_:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def world(pg_dsn):
    """Zone hierarchy parent > child, one category with two sub-categories — same shape as
    the buyer-side `world` fixture, reused here for the producer side's own taxonomy filters."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        region = insert(cur, "governance.climatic_regions", name=uniq("region"))
        child = insert(cur, "governance.zones", name=uniq("child"), code=uniq("C"), climatic_region_id=region, parent_id=g.zone)
        cur.execute("select category_id from governance.sub_categories where id = %s", (g.sub_category,))
        category = cur.fetchone()[0]
        cur.execute("select name from governance.zones where id = %s", (g.zone,))
        parent_name = cur.fetchone()[0]
        extra_producer = g.extra_producer()
    conn.close()
    return SimpleNamespace_(dsn=pg_dsn, g=g, parent=str(g.zone), child=str(child), category=str(category),
                            sub1=str(g.sub_category), parent_name=parent_name, producer2=str(extra_producer))


def _pdm(w, day, producer, zone, *, delivered_direct=0, confirmed_direct=0, gmv_direct=0):
    _sql(w.dsn,
         "insert into analytics.producer_daily_metrics (metric_date, producer_id, zone_id, orders_confirmed_direct, "
         "orders_delivered_direct, delivered_gmv_direct) values (%s,%s,%s,%s,%s,%s)",
         (day, producer, zone, confirmed_direct, delivered_direct, gmv_direct))


def _pqm(w, day, producer, unit, *, confirmed_direct=0, delivered_direct=0):
    family = "COUNT" if unit == "TETE" else "MASS"
    _sql(w.dsn,
         "insert into analytics.producer_quantity_daily_metrics (metric_date, producer_id, canonical_unit, measurement_family, "
         "confirmed_quantity_direct, delivered_quantity_direct) values (%s,%s,%s,%s,%s,%s)",
         (day, producer, unit, family, confirmed_direct, delivered_direct))


def _supply(w, day, producer, zone, category, sub_category, unit, *, quantity=0, count=1):
    family = "COUNT" if unit == "TETE" else "MASS"
    _sql(w.dsn,
         "insert into analytics.producer_supply_daily_snapshot (metric_date, producer_id, zone_id, category_id, "
         "sub_category_id, canonical_unit, measurement_family, available_quantity, product_count) "
         "values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
         (day, producer, zone, category, sub_category, unit, family, quantity, count))


def test_filter_options_are_the_same_real_taxonomy_reader_as_the_buyer_side(world):
    w = world
    opts = _run(w.dsn, api.filter_options)
    assert any(c["id"] == w.category for c in opts["categories"])
    assert w.sub1 in {s["id"] for s in opts["sub_categories"]}


def test_a_parent_zone_filter_includes_descendants_for_active_producers_and_gmv(world):
    w = world
    d = _day()
    _pdm(w, d, str(w.g.producer), w.child, delivered_direct=1, confirmed_direct=1, gmv_direct=1000)
    _pdm(w, d, w.producer2, w.parent, delivered_direct=2, confirmed_direct=2, gmv_direct=3000)
    parent = _run(w.dsn, lambda s: api.overview(s, _params(d, zone_id=w.parent)))
    child = _run(w.dsn, lambda s: api.overview(s, _params(d, zone_id=w.child)))
    assert parent["metrics"]["active_producers"]["value"] == 2
    assert child["metrics"]["active_producers"]["value"] == 1
    assert parent["metrics"]["producer_delivered_gmv"]["value"] == pytest.approx(4000)
    assert child["metrics"]["producer_delivered_gmv"]["value"] == pytest.approx(1000)
    with pytest.raises(api.ApiError):
        _run(w.dsn, lambda s: api.overview(s, _params(d, zone_id=str(uuid.uuid4()))))


def test_quantity_fulfillment_mixed_units_never_one_global_number(world):
    w = world
    d = _day()
    _pqm(w, d, str(w.g.producer), "KG", confirmed_direct=250, delivered_direct=200)
    _pqm(w, d, w.producer2, "TETE", confirmed_direct=12, delivered_direct=10)
    out = _run(w.dsn, lambda s: api.fulfillment(s, _params(d)))
    qty = out["metrics"]["producer_quantity_fulfillment_rate"]
    assert qty["status"] == "MIXED_UNITS" and qty["value"] is None and qty["numerator"] is None
    assert {b["canonical_unit"]: b["numerator"] for b in qty["breakdown"]} == {"KG": 200, "TETE": 10}


def test_a_zone_filter_marks_quantity_fulfillment_unavailable_not_zero(world):
    """producer_quantity_daily_metrics has no zone_id dimension at all (§ TABLE_DIMENSIONS_PRODUCER) —
    requesting it with a zone filter must report UNAVAILABLE with the reason, never a silent 0."""
    w = world
    d = _day()
    _pqm(w, d, str(w.g.producer), "KG", confirmed_direct=100, delivered_direct=80)
    out = _run(w.dsn, lambda s: api.fulfillment(s, _params(d, zone_id=w.parent)))
    qty = out["metrics"]["producer_quantity_fulfillment_rate"]
    assert qty["status"] == "UNAVAILABLE" and qty["value"] is None
    assert "Not available" in qty["notes"][0]
    assert "producer_quantity_fulfillment_rate is DIRECT+RECURRING only" in out["notes"][0]


def test_available_supply_is_a_live_gauge_scoped_by_category_and_filtered_by_zone(world):
    w = world
    today = date.today()
    _supply(w, today, str(w.g.producer), w.parent, w.category, w.sub1, "KG", quantity=250, count=3)
    out = _run(w.dsn, lambda s: api.supply(s, _params(today, category_id=w.category)))
    assert out["metric"]["value"] == pytest.approx(250) and out["metric"]["unit"] == "KG"
    assert out["metric"]["reliability"] == "RELIABLE"
    # A request window in the past is ignored — the gauge always reads "today".
    stale_window = _run(w.dsn, lambda s: api.supply(s, _params(today - timedelta(days=5), category_id=w.category)))
    assert stale_window["metric"]["value"] == pytest.approx(250)


def test_breakdown_by_zone_for_gmv_and_by_canonical_unit_for_quantity(world):
    w = world
    d = _day()
    _pdm(w, d, str(w.g.producer), w.parent, delivered_direct=3, confirmed_direct=3, gmv_direct=500)
    _pdm(w, d, w.producer2, w.child, delivered_direct=1, confirmed_direct=1, gmv_direct=1500)
    by_zone = _run(w.dsn, lambda s: api.breakdown(s, _params(d, zone_id=w.parent), "producer_delivered_gmv", "zone_id", limit=50, offset=0))
    values = {r["id"]: r["value"] for r in by_zone["rows"]}
    assert values == {w.parent: pytest.approx(500), w.child: pytest.approx(1500)}
    assert all("label" in r for r in by_zone["rows"])

    _pqm(w, d, str(w.g.producer), "KG", confirmed_direct=100, delivered_direct=90)
    _pqm(w, d, w.producer2, "L", confirmed_direct=40, delivered_direct=40)
    by_unit = _run(w.dsn, lambda s: api.breakdown(s, _params(d), "producer_quantity_fulfillment_rate", "canonical_unit", limit=50, offset=0))
    assert {r["canonical_unit"]: r["value"] for r in by_unit["rows"]} == {"KG": pytest.approx(0.9), "L": pytest.approx(1.0)}

    with pytest.raises(api.ApiError):
        _run(w.dsn, lambda s: api.breakdown(s, _params(d), "producer_delivered_gmv", "canonical_unit", limit=50, offset=0))
    with pytest.raises(api.ApiError):
        _run(w.dsn, lambda s: api.breakdown(s, _params(d), "active_producers", "zone_id", limit=50, offset=0))


def test_timeseries_and_compare_for_active_producers_and_gmv(world):
    w = world
    d1 = _day()
    d2 = d1 + timedelta(days=1)
    _pdm(w, d1, str(w.g.producer), w.parent, delivered_direct=1, confirmed_direct=1, gmv_direct=100)
    _pdm(w, d2, str(w.g.producer), w.parent, delivered_direct=1, confirmed_direct=1, gmv_direct=400)
    _pdm(w, d2, w.producer2, w.parent, delivered_direct=1, confirmed_direct=1, gmv_direct=100)
    active_ts = _run(w.dsn, lambda s: api.timeseries(s, _params(d1, d2, zone_id=w.parent), "active_producers", "day"))
    assert [p["value"] for p in active_ts["points"]] == [1, 2]
    gmv_ts = _run(w.dsn, lambda s: api.timeseries(s, _params(d1, d2, zone_id=w.parent), "producer_delivered_gmv", "day"))
    assert [p["value"] for p in gmv_ts["points"]] == [100, 500]
    cmp = _run(w.dsn, lambda s: api.compare(s, _params(d2, d2, zone_id=w.parent), "producer_delivered_gmv", d1, d1))
    assert cmp["current"]["value"] == 500 and cmp["previous"]["value"] == 100 and cmp["delta"] == 400
    with pytest.raises(api.ApiError):
        _run(w.dsn, lambda s: api.timeseries(s, _params(d1, d2), "producer_sell_through_rate", None))


def test_freshness_and_health(world):
    w = world
    d = _day()
    _pdm(w, d, str(w.g.producer), w.parent, delivered_direct=1, confirmed_direct=1, gmv_direct=1)
    fresh = _run(w.dsn, api.freshness)
    assert fresh["stale"] is False and fresh["last_refresh"] is not None
    assert set(fresh["tables"]) == {"producer_daily_metrics", "producer_quantity_daily_metrics", "producer_supply_daily_snapshot"}
    later = datetime.now(timezone.utc) + timedelta(days=3)
    stale = _run(w.dsn, lambda s: api.freshness(s, now=later))
    assert stale["stale"] is True
    h = _run(w.dsn, lambda s: api.health(s, now=later))
    assert h["status"] == "Stale"
    assert _run(w.dsn, api.health)["status"] in ("Healthy", "Warning")


def test_overview_and_breakdown_latency_over_thirty_days(world):
    w = world
    start = _day()
    _sql(w.dsn,
         "insert into analytics.producer_daily_metrics (metric_date, producer_id, zone_id, orders_confirmed_direct, orders_delivered_direct, delivered_gmv_direct) "
         "select %s::date + d, gen_random_uuid(), %s::uuid, 3, 2, 1000 from generate_series(0, 29) d, generate_series(1, 50) s", (start, w.parent))
    end = start + timedelta(days=29)

    async def fn(s):
        t0 = time.perf_counter()
        await api.overview(s, _params(start, end, zone_id=w.parent))
        await api.fulfillment(s, _params(start, end, zone_id=w.parent))
        await api.breakdown(s, _params(start, end, zone_id=w.parent), "producer_delivered_gmv", "zone_id", limit=100, offset=0)
        return time.perf_counter() - t0

    assert _run(w.dsn, fn) < 5.0
