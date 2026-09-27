"""Real-PostgreSQL tests for the Producer Metric Layer (Phase C): recompute
idempotency, late-event cohort updates, distinct active-producer counting,
multi-producer/multi-unit isolation, GMV attribution, repeat-producer rate,
data quality and grain uniqueness. Self-skips locally without SCHEMA_TEST_DSN,
same discipline as every other file here — see `test_analytics_metric_layer_pg.py`,
whose conventions (`world`, `_run`, `_sql`, `_fresh_day`) this file reuses."""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta

import psycopg2
import pytest
from factories import Graph, insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.services.analytics.data_quality import run_producer_metric_quality_checks
from ladini.services.analytics.producer_analytics_service import (
    ProducerAnalyticsService,
)
from ladini.services.analytics.producer_metrics_refresh import (
    ProducerMetricsRefresher,
    snapshot_producer_supply,
)

_DAYS = iter(range(1, 2000))


def _fresh_day() -> date:
    return date(2023, 1, 1) + timedelta(days=next(_DAYS))


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


def _sql(dsn, statement, params=()):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(statement, params)
        out = cur.fetchall() if cur.description else None
    conn.close()
    return out


@pytest.fixture
def world(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
    conn.close()
    return pg_dsn, g


def _recompute(dsn, day):
    return _run(dsn, lambda s: ProducerMetricsRefresher(s).recompute_day(day))


def _direct_order(cur, g, *, day, status="CONFIRMED", delivery_status="DELIVERED", quantity=10, unit="KG", producer=None, price=100):
    pid = producer or g.producer
    product = g.product_for(producer=pid, unit=unit)
    order = insert(
        cur, "marketplace.orders", buyer_id=g.buyer, total_amount=quantity * price, order_type="STANDARD",
        status=status, delivery_status=delivery_status, created_at=datetime(day.year, day.month, day.day, 9),
    )
    insert(cur, "marketplace.order_items", order_id=order, product_id=product, quantity=quantity, price_at_sale=price)
    return order


def _tender_order(cur, g, *, day, delivery_status="DELIVERED", quantity=10, price=100, producer=None):
    pid = producer or g.producer
    auction = g.auction()
    bid = g.bid(auction, producer=pid, offered_price=price)
    order = insert(
        cur, "marketplace.orders", buyer_id=g.buyer, total_amount=quantity * price, auction_id=auction,
        winning_bid_id=bid, delivery_status=delivery_status, created_at=datetime(day.year, day.month, day.day, 9),
    )
    return order


def _recurring_order(cur, g, *, day, delivery_status="RECEIVED", quantity=10, unit="KG", producer=None, price=100):
    pid = producer or g.producer
    product = g.product_for(producer=pid, unit=unit)
    order = insert(
        cur, "marketplace.orders", buyer_id=g.buyer, total_amount=quantity * price, order_type="RECURRING_SUPPLY",
        delivery_status=delivery_status, created_at=datetime(day.year, day.month, day.day, 9),
    )
    insert(cur, "marketplace.order_items", order_id=order, product_id=product, quantity=quantity, price_at_sale=price)
    return order


class TestRecomputeIdempotencyAndLateEvents:
    def test_replay_yields_identical_rows(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _direct_order(cur, g, day=day)
        conn.close()
        first = _recompute(dsn, day)
        second = _recompute(dsn, day)
        assert first == second
        assert first["producer_daily_metrics"] == 1

    def test_a_late_delivery_updates_the_original_cohort_row(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            order = _direct_order(cur, g, day=day, status="CONFIRMED", delivery_status="PENDING")
        conn.close()
        _recompute(dsn, day)
        row = _sql(dsn, "select orders_confirmed_direct, orders_delivered_direct from analytics.producer_daily_metrics where metric_date = %s and producer_id = %s", (day, str(g.producer)))
        assert row[0] == (1, 0)

        _sql(dsn, "update marketplace.orders set delivery_status = 'DELIVERED' where id = %s", (order,))
        _recompute(dsn, day)  # re-run the SAME cohort day, not a new one
        row = _sql(dsn, "select orders_confirmed_direct, orders_delivered_direct from analytics.producer_daily_metrics where metric_date = %s and producer_id = %s", (day, str(g.producer)))
        assert row[0] == (1, 1)


class TestActiveProducersDistinctCount:
    def test_same_producer_active_two_days_counts_once_over_the_window(self, world):
        dsn, g = world
        d1 = _fresh_day()
        d2 = d1 + timedelta(days=1)
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _direct_order(cur, g, day=d1, status="CONFIRMED", delivery_status="PENDING")
            _direct_order(cur, g, day=d2, status="CONFIRMED", delivery_status="PENDING")
        conn.close()
        _recompute(dsn, d1)
        _recompute(dsn, d2)

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("active_producers", d1, d2, compare=False)

        res = _run(dsn, metric)
        assert res.value == 1.0

    def test_a_producer_with_only_a_publish_event_no_order_still_counts(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            insert(
                cur, "analytics.business_events", event_name="PRODUCT_PUBLISHED_FOR_SALE", journey="SUPPLY",
                actor_type="PRODUCER", producer_id=g.producer, entity_type="PRODUCT", entity_id=g.product,
                occurred_at=datetime(day.year, day.month, day.day, 9), idempotency_key=f"idem-{uuid.uuid4()}",
            )
        conn.close()
        _recompute(dsn, day)
        row = _sql(dsn, "select products_published from analytics.producer_daily_metrics where metric_date = %s and producer_id = %s", (day, str(g.producer)))
        assert row == [(1,)]

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("active_producers", day, day, compare=False)

        assert _run(dsn, metric).value == 1.0

    def test_a_producer_with_no_qualifying_fact_is_not_counted(self, world):
        dsn, g = world
        day = _fresh_day()
        _recompute(dsn, day)
        row = _sql(dsn, "select count(*) from analytics.producer_daily_metrics where metric_date = %s and producer_id = %s", (day, str(g.producer)))
        assert row == [(0,)]


class TestMultipleProducersIsolation:
    def test_two_producers_active_the_same_day_get_two_rows(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            p2 = g.extra_producer()
            _direct_order(cur, g, day=day, status="CONFIRMED", producer=g.producer)
            _direct_order(cur, g, day=day, status="CONFIRMED", producer=p2)
        conn.close()
        written = _recompute(dsn, day)
        assert written["producer_daily_metrics"] == 2


class TestQuantityUnitSafety:
    def test_grams_convert_to_the_subcategory_priority_unit(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            cat = insert(cur, "governance.categories", name=f"cat-{uuid.uuid4().hex[:6]}")
            sub = insert(cur, "governance.sub_categories", category_id=cat, name=f"sub-{uuid.uuid4().hex[:6]}", priority_unit="KG")
            product = insert(cur, "marketplace.products", category_label="x", price=100, producer_id=g.producer, sub_category_id=sub, unit="G")
            order = insert(cur, "marketplace.orders", buyer_id=g.buyer, total_amount=100, order_type="STANDARD", status="CONFIRMED", delivery_status="DELIVERED", created_at=datetime(day.year, day.month, day.day, 9))
            insert(cur, "marketplace.order_items", order_id=order, product_id=product, quantity=1500, price_at_sale=100)
        conn.close()
        _recompute(dsn, day)
        row = _sql(dsn, "select canonical_unit, confirmed_quantity_direct from analytics.producer_quantity_daily_metrics where metric_date = %s and producer_id = %s", (day, str(g.producer)))
        assert row == [("KG", 1.5)]

    def test_kg_and_tete_never_summed_into_one_row(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _direct_order(cur, g, day=day, status="CONFIRMED", unit="KG", quantity=100)
            _direct_order(cur, g, day=day, status="CONFIRMED", unit="TETE", quantity=5)
        conn.close()
        _recompute(dsn, day)
        rows = _sql(dsn, "select canonical_unit from analytics.producer_quantity_daily_metrics where metric_date = %s and producer_id = %s order by 1", (day, str(g.producer)))
        assert rows == [("KG",), ("TETE",)]

    def test_tender_never_contributes_a_quantity_row(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _tender_order(cur, g, day=day, delivery_status="DELIVERED", quantity=50)
        conn.close()
        _recompute(dsn, day)
        rows = _sql(dsn, "select count(*) from analytics.producer_quantity_daily_metrics where metric_date = %s and producer_id = %s", (day, str(g.producer)))
        assert rows == [(0,)]

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("producer_quantity_fulfillment_rate", day, day, compare=False)

        res = _run(dsn, metric)
        assert res.status.value == "NO_DATA"  # TENDER-only day: no DIRECT/RECURRING quantity facts at all


class TestGmvAttribution:
    def test_direct_tender_recurring_gmv_all_attribute_to_the_right_producer(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _direct_order(cur, g, day=day, status="CONFIRMED", delivery_status="DELIVERED", quantity=10, price=100)
            _tender_order(cur, g, day=day, delivery_status="DELIVERED", quantity=5, price=200)
            _recurring_order(cur, g, day=day, delivery_status="RECEIVED", quantity=2, price=300)
        conn.close()
        _recompute(dsn, day)
        row = _sql(
            dsn,
            "select delivered_gmv_direct, delivered_gmv_tender, delivered_gmv_recurring from analytics.producer_daily_metrics "
            "where metric_date = %s and producer_id = %s", (day, str(g.producer)),
        )
        assert row == [(1000, 1000, 600)]

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("producer_delivered_gmv", day, day, compare=False)

        res = _run(dsn, metric)
        assert res.value == 2600.0


class TestRepeatProducer:
    def test_two_delivered_orders_same_producer_counts_as_repeat(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _direct_order(cur, g, day=day, status="CONFIRMED", delivery_status="DELIVERED")
            _direct_order(cur, g, day=day, status="CONFIRMED", delivery_status="DELIVERED")
        conn.close()
        _recompute(dsn, day)

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("repeat_producer_rate", day, day, compare=False)

        res = _run(dsn, metric)
        assert res.numerator == 1.0 and res.denominator == 1.0 and res.value == 1.0

    def test_one_delivered_order_is_not_a_repeat(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _direct_order(cur, g, day=day, status="CONFIRMED", delivery_status="DELIVERED")
        conn.close()
        _recompute(dsn, day)

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("repeat_producer_rate", day, day, compare=False)

        res = _run(dsn, metric)
        assert res.numerator == 0.0 and res.denominator == 1.0 and res.value == 0.0


class TestZeroDenominatorAndFulfillment:
    def test_confirmed_but_never_delivered_yields_zero_fulfillment_not_null(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _direct_order(cur, g, day=day, status="CONFIRMED", delivery_status="PENDING")
        conn.close()
        _recompute(dsn, day)

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("producer_order_fulfillment_rate", day, day, compare=False)

        res = _run(dsn, metric)
        assert res.value == 0.0  # 0 delivered / 1 confirmed — a real ratio, not NO_DATA

    def test_no_confirmed_orders_at_all_yields_null_not_zero(self, world):
        dsn, g = world
        day = _fresh_day()
        _recompute(dsn, day)

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("producer_order_fulfillment_rate", day, day, compare=False)

        res = _run(dsn, metric)
        assert res.value is None and res.status.value == "NO_DATA"


class TestDataQualityAndGrainUniqueness:
    def test_clean_day_raises_no_issue(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            _direct_order(cur, g, day=day, status="CONFIRMED", delivery_status="DELIVERED")
        conn.close()
        _recompute(dsn, day)
        issues = _run(dsn, lambda s: run_producer_metric_quality_checks(s, day, day))
        assert not any(i.check.startswith("delivered_gt_confirmed") for i in issues)

    def test_duplicate_grain_is_rejected_by_the_unique_index(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        try:
            insert(cur, "analytics.producer_daily_metrics", metric_date=day, producer_id=g.producer)
            cur.execute("SAVEPOINT dup")
            with pytest.raises(psycopg2.errors.UniqueViolation):
                insert(cur, "analytics.producer_daily_metrics", metric_date=day, producer_id=g.producer)
            cur.execute("ROLLBACK TO SAVEPOINT dup")
            conn.commit()
        finally:
            conn.close()


class TestSupplySnapshot:
    def test_refuses_a_past_day(self, world):
        dsn, _g = world

        async def go(s):
            return await snapshot_producer_supply(s, date(2020, 1, 1))

        with pytest.raises(ValueError):
            _run(dsn, go)

    def test_snapshots_todays_sellable_products_and_is_idempotent(self, world):
        dsn, g = world
        today = date.today()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            g.product_for(quantity_for_sale=100, is_available=True, unit="KG")
            g.product_for(quantity_for_sale=0, is_available=False, unit="KG")  # paused: excluded
        conn.close()

        async def go(s):
            return await snapshot_producer_supply(s, today)

        n1 = _run(dsn, go)
        n2 = _run(dsn, go)
        assert n1 == n2 and n1 >= 1
        row = _sql(dsn, "select available_quantity from analytics.producer_supply_daily_snapshot where metric_date = %s and producer_id = %s", (today, str(g.producer)))
        assert row and float(row[0][0]) == 100.0

    def test_available_supply_metric_reads_the_latest_snapshot(self, world):
        dsn, g = world
        today = date.today()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            g.product_for(quantity_for_sale=250, is_available=True, unit="KG")
        conn.close()
        _run(dsn, lambda s: snapshot_producer_supply(s, today))

        async def metric(s):
            return await ProducerAnalyticsService(s).get_metric("available_supply", today, today, compare=False)

        res = _run(dsn, metric)
        assert res.value == 250.0 and res.unit == "KG"
