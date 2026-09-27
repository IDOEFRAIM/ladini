"""Phase E - the analytics API payload builders against a real PostgreSQL (CI): zone hierarchy,
taxonomy labels, unmatched demand, breakdowns, series, freshness/health and a latency guard."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.services.analytics.admin_api as api

_DAYS = iter(range(1, 2000))


def _day() -> date:
    return date(2019, 1, 1) + timedelta(days=next(_DAYS))


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


@pytest.fixture
def world(pg_dsn):
    """Zone hierarchy parent > child, one category with two sub-categories (real rows, real names)."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        region = insert(cur, "governance.climatic_regions", name=uniq("region"))
        child = insert(cur, "governance.zones", name=uniq("child"), code=uniq("C"), climatic_region_id=region, parent_id=g.zone)
        cur.execute("select category_id from governance.sub_categories where id = %s", (g.sub_category,))
        category = cur.fetchone()[0]
        cur.execute("update governance.sub_categories set name = %s where id = %s", (uniq("Tomate"), g.sub_category))
        sub2 = insert(cur, "governance.sub_categories", category_id=category, name=uniq("Oignon"))
        cur.execute("select name from governance.zones where id = %s", (g.zone,))
        parent_name = cur.fetchone()[0]
    conn.close()
    return SimpleNamespace_(dsn=pg_dsn, g=g, parent=str(g.zone), child=str(child), category=str(category),
                            sub1=str(g.sub_category), sub2=str(sub2), parent_name=parent_name)


class SimpleNamespace_:  # tiny attribute bag (keeps the fixture readable)
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _rec(w, day, zone, sub, unit="KG", requested=100, matched=60, confirmed=0, delivered=0):
    family = "VOLUME" if unit == "L" else ("COUNT" if unit == "TETE" else "MASS")
    _sql(w.dsn,
         "insert into analytics.recurring_daily_metrics (metric_date, zone_id, category_id, sub_category_id, canonical_unit, measurement_family, "
         "occurrences_active, requested_quantity, matched_quantity, unmatched_quantity, confirmed_quantity, delivered_quantity) "
         "values (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s)",
         (day, zone, w.category, sub, unit, family, requested, matched, max(requested - matched, 0), confirmed, delivered))


def test_filter_options_come_from_the_real_taxonomy(world):
    w = world
    opts = _run(w.dsn, api.filter_options)
    assert any(c["id"] == w.category for c in opts["categories"])
    assert {s["id"] for s in opts["sub_categories"]} >= {w.sub1, w.sub2}
    zone = next(z for z in opts["zones"] if z["id"] == w.child)
    assert zone["parent_id"] == w.parent


def test_a_parent_zone_filter_includes_its_descendants_but_a_child_filter_does_not_include_the_parent(world):
    w = world
    d = _day()
    _rec(w, d, w.child, w.sub1, requested=100, matched=50)
    _rec(w, d, w.parent, w.sub1, requested=100, matched=100)
    parent = _run(w.dsn, lambda s: api.recurring(s, _params(d, zone_id=w.parent)))
    child = _run(w.dsn, lambda s: api.recurring(s, _params(d, zone_id=w.child)))
    assert parent["metrics"]["recurring_requested_quantity"]["numerator"] == 200
    assert child["metrics"]["recurring_requested_quantity"]["numerator"] == 100
    assert child["metrics"]["recurring_coverage_rate"]["value"] == pytest.approx(0.5)
    assert parent["metrics"]["recurring_coverage_rate"]["value"] == pytest.approx(0.75)
    with pytest.raises(api.ApiError):
        _run(w.dsn, lambda s: api.recurring(s, _params(d, zone_id=str(uuid.uuid4()))))


def test_recurring_mixed_units_return_a_breakdown_never_one_global_number(world):
    w = world
    d = _day()
    _rec(w, d, w.parent, w.sub1, "KG", 4200, 3000)
    _rec(w, d, w.parent, w.sub2, "L", 850, 850)
    _rec(w, d, w.parent, w.sub2, "TETE", 73, 20)
    out = _run(w.dsn, lambda s: api.recurring(s, _params(d, zone_id=w.parent)))
    req = out["metrics"]["recurring_requested_quantity"]
    assert req["status"] == "MIXED_UNITS" and req["numerator"] is None and req["value"] is None
    assert {b["canonical_unit"]: b["numerator"] for b in req["breakdown"]} == {"KG": 4200, "L": 850, "TETE": 73}
    assert out["metrics"]["recurring_coverage_rate"]["status"] == "MIXED_UNITS"
    assert out["metrics"]["recurring_delivered_quantity"]["reliability"] == "PARTIAL"
    assert {n["metric_name"] for n in out["not_available"]} == {"active_recurring_needs", "recurring_modification_rate"}


def test_unmatched_demand_is_labelled_sorted_and_separate_from_undelivered(world):
    w = world
    d = _day()
    _rec(w, d, w.parent, w.sub1, "KG", requested=1500, matched=260, confirmed=260, delivered=100)   # unmatched 1240, undelivered 160
    _rec(w, d, w.child, w.sub2, "KG", requested=500, matched=70, confirmed=70, delivered=70)        # unmatched 430
    _rec(w, d, w.parent, w.sub2, "TETE", requested=18, matched=0)                                    # unmatched 18 TETE
    out = _run(w.dsn, lambda s: api.unmatched_demand(s, _params(d, zone_id=w.parent), limit=50, offset=0))
    rows = out["unmatched"]["rows"]
    assert [(r["canonical_unit"], r["unmatched"]) for r in rows] == [("KG", 1240.0), ("KG", 430.0), ("TETE", 18.0)]
    assert rows[0]["zone"] == w.parent_name and rows[0]["sub_category"].startswith("Tomate") and rows[0]["category_id"] == w.category
    assert rows[0]["coverage"] == pytest.approx(260 / 1500)
    assert "NOT undelivered" in out["unmatched"]["definition"]
    assert [(r["sub_category_id"], r["undelivered_confirmed"]) for r in out["undelivered_confirmed"]["rows"]] == [(w.sub1, 160.0)]
    assert out["undelivered_confirmed"]["reliability"] == "PARTIAL"
    assert {t["canonical_unit"] for t in out["totals_by_unit"]} == {"KG", "TETE"}
    paged = _run(w.dsn, lambda s: api.unmatched_demand(s, _params(d, zone_id=w.parent), limit=1, offset=1))
    assert paged["unmatched"]["total"] == 3 and len(paged["unmatched"]["rows"]) == 1


def test_breakdown_by_category_then_subcategory_with_labels_and_hierarchy(world):
    w = world
    d = _day()
    _rec(w, d, w.parent, w.sub1, "KG", 100, 84)
    _rec(w, d, w.parent, w.sub2, "KG", 100, 41)
    by_cat = _run(w.dsn, lambda s: api.breakdown(s, _params(d, zone_id=w.parent), "recurring_coverage_rate", "category_id", limit=50, offset=0))
    assert by_cat["rows"][0]["id"] == w.category and by_cat["rows"][0]["value"] == pytest.approx(125 / 200)
    by_sub = _run(w.dsn, lambda s: api.breakdown(s, _params(d, zone_id=w.parent, category_id=w.category), "recurring_coverage_rate", "sub_category_id", limit=50, offset=0))
    values = {r["id"]: r["value"] for r in by_sub["rows"]}
    assert values == {w.sub1: pytest.approx(0.84), w.sub2: pytest.approx(0.41)}
    assert all(r["category_id"] == w.category and r["canonical_unit"] == "KG" for r in by_sub["rows"])
    by_zone = _run(w.dsn, lambda s: api.breakdown(s, _params(d, zone_id=w.parent), "recurring_coverage_rate", "zone_id", limit=50, offset=0))
    assert by_zone["rows"][0]["label"] == w.parent_name
    with pytest.raises(api.ApiError):
        _run(w.dsn, lambda s: api.breakdown(s, _params(d), "needs_created", "category_id", limit=5, offset=0))  # buyer table has no category


def test_overview_with_a_category_filter_marks_buyer_level_metrics_unavailable_not_zero(world):
    w = world
    d = _day()
    _sql(w.dsn, "insert into analytics.buyer_daily_metrics (metric_date, buyer_id, zone_id, needs_direct, satisfied_direct) values (%s,%s,%s,2,1)",
         (d, str(w.g.buyer), w.parent))
    plain = _run(w.dsn, lambda s: api.overview(s, _params(d, zone_id=w.parent)))
    assert plain["metrics"]["needs_created"]["value"] == 2
    spr = plain["metrics"]["successful_procurement_rate"]
    assert spr["status"] == "PARTIAL" and spr["reliability"] == "PARTIAL" and spr["value"] == pytest.approx(0.5)
    assert {m["journey"]: m["needs"] for m in plain["journey_mix"]} == {"DIRECT": 2, "TENDER": 0, "RECURRING": 0}
    assert plain["metrics"]["active_buyers"]["value"] == 1
    filtered = _run(w.dsn, lambda s: api.overview(s, _params(d, zone_id=w.parent, category_id=w.category)))
    assert filtered["metrics"]["needs_created"]["status"] == "UNAVAILABLE" and filtered["metrics"]["needs_created"]["value"] is None
    assert any(n["metric_name"] == "recurring_modification_rate" for n in plain["not_available"])


def test_series_active_buyers_spr_and_compare(world):
    w = world
    d1 = _day()
    d2 = d1 + timedelta(days=1)
    for d, needs, sat in ((d1, 4, 1), (d2, 2, 2)):
        _sql(w.dsn, "insert into analytics.buyer_daily_metrics (metric_date, buyer_id, zone_id, needs_direct, satisfied_direct) values (%s,%s,%s,%s,%s)",
             (d, str(w.g.buyer), w.parent, needs, sat))
    ts = _run(w.dsn, lambda s: api.timeseries(s, _params(d1, d2, zone_id=w.parent), "successful_procurement_rate", "day"))
    assert [(p["numerator"], p["denominator"]) for p in ts["points"]] == [(1, 4), (2, 2)]
    active = _run(w.dsn, lambda s: api.timeseries(s, _params(d1, d2, zone_id=w.parent), "active_buyers", "week"))
    assert sum(p["value"] for p in active["points"]) >= 1
    cmp = _run(w.dsn, lambda s: api.compare(s, _params(d2, d2, zone_id=w.parent), "needs_created", d1, d1))
    assert cmp["current"]["value"] == 2 and cmp["previous"]["value"] == 4 and cmp["delta"] == -2
    default_prev = _run(w.dsn, lambda s: api.compare(s, _params(d2, d2, zone_id=w.parent), "needs_created", None, None))
    assert default_prev["previous_period"] == {"from": d1.isoformat(), "to": d1.isoformat()} and default_prev["previous"]["value"] == 4


def test_freshness_and_health(world):
    w = world
    d = _day()
    _rec(w, d, w.parent, w.sub1)  # a fresh computed_at
    fresh = _run(w.dsn, api.freshness)
    assert fresh["stale"] is False and fresh["last_refresh"] is not None
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
         "insert into analytics.recurring_daily_metrics (metric_date, zone_id, sub_category_id, canonical_unit, measurement_family, occurrences_active, requested_quantity, matched_quantity) "
         "select %s::date + d, %s::uuid, gen_random_uuid(), 'KG', 'MASS', 3, 100, 60 from generate_series(0, 29) d, generate_series(1, 50) s", (start, w.parent))
    end = start + timedelta(days=29)

    async def fn(s):
        t0 = time.perf_counter()
        await api.overview(s, _params(start, end, zone_id=w.parent))
        await api.recurring(s, _params(start, end, zone_id=w.parent))
        await api.breakdown(s, _params(start, end, zone_id=w.parent), "recurring_coverage_rate", "sub_category_id", limit=100, offset=0)
        return time.perf_counter() - t0

    assert _run(w.dsn, fn) < 5.0
