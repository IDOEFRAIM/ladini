"""Phase D against a real PostgreSQL (CI): the refresher's SQL, idempotent recompute, late events,
the metric layer's SQL, targets, data quality and a light performance guard.

Isolation: the database is shared. Every test works on its own PAST day (`_fresh_day`, so the
"no future day" guard never trips and other suites' rows created today are never read) and its
own zone (all reads are zone-filtered)."""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, datetime, timedelta

import psycopg2
import pytest
from factories import Graph, insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.services.analytics.analytics_service import AnalyticsService
from ladini.services.analytics.daily_metrics_refresh import DailyMetricsRefresher
from ladini.services.analytics.data_quality import run_data_quality_checks

_DAYS = iter(range(1, 2000))


def _fresh_day() -> date:
    return date(2023, 1, 1) + timedelta(days=next(_DAYS))


def _dt(day: date, hour: int = 10) -> datetime:
    return datetime(day.year, day.month, day.day, hour)


def _run(dsn, fn):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _recompute(dsn, day):
    return _run(dsn, lambda s: DailyMetricsRefresher(s).recompute_day(day))


def _snapshot(dsn, table, day, zone):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(f"select * from analytics.{table} where metric_date = %s and zone_id = %s order by id", (day, str(zone)))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        r.pop("id"), r.pop("computed_at")
    return sorted(rows, key=lambda r: str(r))


@pytest.fixture
def world(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        cur.execute("update auth.users set zone_id = %s where id = %s", (str(g.zone), str(g.buyer_user)))
    conn.close()
    return pg_dsn, g


def _sql(dsn, statement, params=()):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(statement, params)
        out = cur.fetchall() if cur.description else None
    conn.close()
    return out


def _seed_day(dsn, g, day):
    """2 direct orders (1 delivered), 2 auctions (1 with bid+winner+delivered order), 3 recurring occurrences."""
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        o1 = g.order(zone_id=g.zone, created_at=_dt(day), delivery_status="DELIVERED", total_amount=2000)
        insert(cur, "marketplace.order_items", order_id=o1, product_id=g.product, quantity=2, price_at_sale=1000)
        o2 = g.order(zone_id=g.zone, created_at=_dt(day, 11), total_amount=1000)
        insert(cur, "marketplace.order_items", order_id=o2, product_id=g.product, quantity=1, price_at_sale=1000)
        a1 = g.auction(target_zone_id=g.zone, created_at=_dt(day), quantity=10, max_price_per_unit=500)
        g.auction(target_zone_id=g.zone, created_at=_dt(day, 12))
        b1 = g.bid(a1, created_at=_dt(day, 10) + timedelta(minutes=10))
        cur.execute("update marketplace.auctions set winner_bid_id = %s where id = %s", (b1, a1))
        g.order(zone_id=g.zone, auction_id=a1, created_at=_dt(day, 13), delivery_status="DELIVERED", total_amount=4500)
        need = g.recurring_need(quantity=100, unit="KG")
        g.occurrence(need, occurrence_date=_dt(day, 0), requested_quantity=100, unit="KG", quantity_matched=100, status="ACCEPTED", notified_at=_dt(day, 6))
        other_need = g.recurring_need(quantity=100, unit="KG")
        g.occurrence(other_need, occurrence_date=_dt(day, 0), requested_quantity=100, unit="KG", quantity_matched=40)
    conn.close()
    return a1


# ── recompute: correctness + idempotence ───────────────────────────────

def test_recompute_builds_the_four_tables_and_a_replay_is_identical(world):
    dsn, g = world
    day = _fresh_day()
    _seed_day(dsn, g, day)
    _recompute(dsn, day)
    first = {t: _snapshot(dsn, t, day, g.zone) for t in ("direct_daily_metrics", "tender_daily_metrics", "recurring_daily_metrics", "buyer_daily_metrics")}
    _recompute(dsn, day)
    second = {t: _snapshot(dsn, t, day, g.zone) for t in first}
    assert first == second  # same rows, same values, no duplicates

    direct = next(r for r in first["direct_daily_metrics"] if r["orders_created"])
    assert (direct["orders_created"], direct["orders_delivered"]) == (2, 1) and float(direct["delivered_value"]) == 2000
    tender = first["tender_daily_metrics"][0]
    assert (tender["tenders_created"], tender["tenders_with_bid"], tender["tenders_with_winner"], tender["tender_orders_delivered"]) == (2, 1, 1, 1)
    assert float(tender["first_bid_latency_seconds_sum"]) == 600 and tender["first_bid_latency_count"] == 1
    rec = first["recurring_daily_metrics"][0]
    assert rec["canonical_unit"] == "KG" and (rec["occurrences_active"], rec["occurrences_fully_covered"]) == (2, 1)
    assert float(rec["requested_quantity"]) == 200 and float(rec["matched_quantity"]) == 140 and float(rec["unmatched_quantity"]) == 60
    buyer = first["buyer_daily_metrics"][0]
    assert (buyer["needs_direct"], buyer["satisfied_direct"], buyer["needs_tender"], buyer["satisfied_tender"], buyer["needs_recurring"]) == (2, 1, 2, 1, 2)


def test_late_delivery_changes_the_original_cohort_row_at_recompute(world):
    dsn, g = world
    day = _fresh_day()
    _seed_day(dsn, g, day)
    _recompute(dsn, day)
    before = _snapshot(dsn, "direct_daily_metrics", day, g.zone)[0]["orders_delivered"]
    _sql(dsn, "update marketplace.orders set delivery_status = 'DELIVERED' where zone_id = %s and created_at = %s and auction_id is null and delivery_status <> 'DELIVERED'",
         (str(g.zone), _dt(day, 11)))
    _recompute(dsn, day)  # late arrival: the cohort of `day` is recomputed
    after = _snapshot(dsn, "direct_daily_metrics", day, g.zone)[0]["orders_delivered"]
    assert (before, after) == (1, 2)


def test_future_days_are_refused(world):
    dsn, _g = world
    with pytest.raises(ValueError):
        _recompute(dsn, date.today() + timedelta(days=2))


# ── recurring RECEIVED relation (occurrence level) ─────────────────────

def test_occurrence_is_received_only_when_all_its_orders_are_received(world):
    dsn, g = world
    day = _fresh_day()
    group = uuid.uuid4()
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        need = g.recurring_need(quantity=50, unit="KG")
        g.occurrence(need, occurrence_date=_dt(day, 0), requested_quantity=50, unit="KG", quantity_matched=50,
                     quantity_confirmed=50, status="ACCEPTED", order_group_id=str(group))
        for delivery in ("RECEIVED", "DELIVERED"):
            g.order(zone_id=g.zone, checkout_group_id=str(group), order_type="RECURRING_SUPPLY", delivery_status=delivery, total_amount=1000)
    conn.close()
    _recompute(dsn, day)
    r = _snapshot(dsn, "recurring_daily_metrics", day, g.zone)[0]
    assert (r["occurrences_with_orders"], r["occurrences_all_received"]) == (1, 0)
    _sql(dsn, "update marketplace.orders set delivery_status = 'RECEIVED' where checkout_group_id = %s", (str(group),))
    _recompute(dsn, day)
    r = _snapshot(dsn, "recurring_daily_metrics", day, g.zone)[0]
    assert r["occurrences_all_received"] == 1 and float(r["received_value"]) == 2000
    b = _snapshot(dsn, "buyer_daily_metrics", day, g.zone)[0]
    assert b["satisfied_recurring"] == 1


# ── metric layer SQL ───────────────────────────────────────────────────

def _insert_recurring_row(dsn, g, day, *, unit="KG", requested=1000, matched=800, family="MASS", sub=None):
    _sql(dsn,
         "insert into analytics.recurring_daily_metrics (metric_date, zone_id, sub_category_id, canonical_unit, measurement_family, requested_quantity, matched_quantity) "
         "values (%s, %s, %s, %s, %s, %s, %s)", (day, str(g.zone), str(sub or uuid.uuid4()), unit, family, requested, matched))


def test_weighted_rate_is_sum_over_sum_not_an_average(world):
    dsn, g = world
    day = _fresh_day()
    _insert_recurring_row(dsn, g, day, requested=1000, matched=800)
    _insert_recurring_row(dsn, g, day, requested=100, matched=20)
    res = _run(dsn, lambda s: AnalyticsService(s).get_metric("recurring_coverage_rate", day, day, filters={"zone_id": g.zone}, compare=False))
    assert (res.numerator, res.denominator) == (820, 1100)
    assert res.value == pytest.approx(820 / 1100) and res.value != pytest.approx(0.5)
    assert res.unit == "ratio" and res.status.value == "OK"


def test_incompatible_units_are_never_summed(world):
    dsn, g = world
    day = _fresh_day()
    _insert_recurring_row(dsn, g, day, unit="KG", requested=100, matched=50)
    _insert_recurring_row(dsn, g, day, unit="L", requested=500, matched=500, family="VOLUME")
    res = _run(dsn, lambda s: AnalyticsService(s).get_metric("recurring_coverage_rate", day, day, filters={"zone_id": g.zone}, compare=False))
    assert res.status.value == "MIXED_UNITS" and res.value is None and res.numerator is None
    assert {b["canonical_unit"]: b["value"] for b in res.breakdown} == {"KG": 0.5, "L": 1.0}
    unmatched = _run(dsn, lambda s: AnalyticsService(s).get_unfulfilled_demand(day, day, filters={"zone_id": g.zone}))
    assert unmatched["kind"] == "UNMATCHED_DEMAND"


def test_zero_denominator_yields_null_value(world):
    dsn, g = world
    day = _fresh_day()
    _sql(dsn, "insert into analytics.tender_daily_metrics (metric_date, zone_id) values (%s, %s)", (day, str(g.zone)))
    res = _run(dsn, lambda s: AnalyticsService(s).get_metric("tender_response_rate", day, day, filters={"zone_id": g.zone}, compare=False))
    assert res.value is None and res.status.value == "NO_DATA"


def test_target_resolution_and_timeseries_and_breakdown(world):
    dsn, g = world
    day = _fresh_day()
    for offset, (req, mat) in enumerate([(100, 50), (100, 90)]):
        _insert_recurring_row(dsn, g, day + timedelta(days=offset), requested=req, matched=mat)
    _sql(dsn, "insert into analytics.metric_targets (metric_name, scope_type, target_value, warning_threshold, critical_threshold, valid_from) values ('recurring_coverage_rate','GLOBAL',0.9,0.7,0.5,'2020-01-01')")
    _sql(dsn, "insert into analytics.metric_targets (metric_name, scope_type, scope_id, target_value, valid_from) values ('recurring_coverage_rate','ZONE',%s,0.6,'2020-01-01')", (str(g.zone),))

    async def fn(s):
        svc = AnalyticsService(s)
        res = await svc.get_metric("recurring_coverage_rate", day, day + timedelta(days=1), filters={"zone_id": g.zone}, compare=False)
        series = await svc.get_metric_timeseries("recurring_coverage_rate", day, day + timedelta(days=1), filters={"zone_id": g.zone})
        weekly = await svc.get_metric_timeseries("recurring_coverage_rate", day, day + timedelta(days=1), granularity="week", filters={"zone_id": g.zone})
        by_zone = await svc.get_metric_breakdown("recurring_coverage_rate", day, day + timedelta(days=1), dimension="zone_id", filters={"zone_id": g.zone})
        return res, series, weekly, by_zone

    res, series, weekly, by_zone = _run(dsn, fn)
    assert res.value == pytest.approx(0.7)
    assert res.target["scope_type"] == "ZONE" and res.target["value"] == 0.6  # ZONE beats GLOBAL when filtered on the zone
    assert res.target_status.value == "ON_TARGET"
    assert [round(p["value"], 2) for p in series] == [0.5, 0.9]
    # whatever the bucketing, parts are summed (never averaged): 100+100 requested, 50+90 matched
    assert sum(p["numerator"] for p in weekly) == 140 and sum(p["denominator"] for p in weekly) == 200
    assert by_zone[0]["value"] == pytest.approx(0.7)


def test_active_buyers_are_distinct_across_days(world):
    dsn, g = world
    d1 = _fresh_day()
    d2 = d1 + timedelta(days=1)
    for d in (d1, d2):
        _sql(dsn, "insert into analytics.buyer_daily_metrics (metric_date, buyer_id, zone_id, needs_direct) values (%s, %s, %s, 1)", (d, str(g.buyer), str(g.zone)))
    res = _run(dsn, lambda s: AnalyticsService(s).get_metric("active_buyers", d1, d2, filters={"zone_id": g.zone}, compare=False))
    assert res.value == 1  # same buyer on two days = one active buyer


# ── data quality ───────────────────────────────────────────────────────

def test_data_quality_flags_missing_days_and_unknown_units_then_clears_after_recompute(world):
    dsn, g = world
    day = _fresh_day()
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        need = g.recurring_need(quantity=5, unit="BIDON")
        g.occurrence(need, occurrence_date=_dt(day, 0), requested_quantity=5, unit="BIDON")
    conn.close()

    issues = _run(dsn, lambda s: run_data_quality_checks(s, day, day))
    assert any(i.check == "missing_aggregate_days" and i.table.endswith("recurring_daily_metrics") for i in issues)
    _recompute(dsn, day)
    issues = _run(dsn, lambda s: run_data_quality_checks(s, day, day))
    assert not any(i.check == "missing_aggregate_days" and i.table.endswith("recurring_daily_metrics") for i in issues)
    assert any(i.check == "unknown_canonical_unit" and "BIDON" in i.detail for i in issues)
    assert not any(i.check == "duplicate_grain" for i in issues)


def test_duplicate_grain_is_rejected_by_the_unique_index(world):
    dsn, g = world
    day = _fresh_day()
    stmt = "insert into analytics.tender_daily_metrics (metric_date, zone_id) values (%s, %s)"
    _sql(dsn, stmt, (day, str(g.zone)))
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _sql(dsn, stmt, (day, str(g.zone)))


# ── performance guard ──────────────────────────────────────────────────

def test_reads_over_thirty_days_of_aggregates_stay_fast(world):
    dsn, g = world
    start = _fresh_day()
    _sql(dsn,
         "insert into analytics.recurring_daily_metrics (metric_date, zone_id, sub_category_id, canonical_unit, measurement_family, occurrences_total, occurrences_active, requested_quantity, matched_quantity) "
         "select %s::date + d, %s::uuid, gen_random_uuid(), 'KG', 'MASS', 5, 5, 100, 60 from generate_series(0, 29) d, generate_series(1, 100) s",
         (start, str(g.zone)))
    end = start + timedelta(days=29)

    async def fn(s):
        svc = AnalyticsService(s)
        t0 = time.perf_counter()
        await svc.get_metric("recurring_coverage_rate", start, end, filters={"zone_id": g.zone})
        await svc.get_metric_breakdown("recurring_matched_quantity", start, end, dimension="sub_category_id", filters={"zone_id": g.zone})
        await svc.get_buyer_overview(start, end, filters={"zone_id": g.zone})
        await svc.get_tender_metrics(start, end, filters={"zone_id": g.zone})
        return time.perf_counter() - t0

    elapsed = _run(dsn, fn)
    print(f"PERF analytics reads over 3000 recurring rows / 30d: {elapsed:.3f}s")
    assert elapsed < 3.0
