"""Phase D.5 against a real PostgreSQL (CI): the recurring `quantity_delivered` writer and its
aggregates, the DIRECT lifecycle events, and the reshaped DIRECT cohort (PREORDER checkout)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph, insert
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.services.analytics.analytics_service import AnalyticsService
from ladini.services.analytics.daily_metrics_refresh import (
    DailyMetricsRefresher,
    backfill_quantity_delivered,
)
from ladini.services.analytics.data_quality import run_data_quality_checks
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin

_DAYS = iter(range(1, 2000))


def _fresh_day() -> date:
    return date(2021, 1, 1) + timedelta(days=next(_DAYS))


def _dt(day: date, hour: int = 10) -> datetime:
    return datetime(day.year, day.month, day.day, hour)


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin):
    def __init__(self, session, user, profile):
        self._s, self._user, self._profile = session, user, profile

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._user, self._profile

    async def get_producer_profile(self, phone):
        return self._user, self._profile


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
        cur.execute("update auth.users set zone_id = %s where id = %s", (str(g.zone), str(g.buyer_user)))
    conn.close()
    return pg_dsn, g


def _buyer(g):
    return SimpleNamespace(id=g.buyer_user, zone_id=g.zone), SimpleNamespace(id=g.buyer)


# ── recurring: occurrence -> orders -> items -> RECEIVED -> quantity_delivered ──────────────

def _accepted_occurrence(dsn, g, day):
    """Occurrence (50 KG requested) matched by TWO producers (30 + 20), accepted for real."""
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        need = g.recurring_need(quantity=50, unit="KG")
        occ = g.occurrence(need, occurrence_date=_dt(day, 0), requested_quantity=50, unit="KG", status="MATCHED", quantity_matched=50)
        p1 = g.product_for(quantity_for_sale=100)
        second_producer = g.extra_producer()
        p2 = g.product_for(producer=second_producer, quantity_for_sale=100)
        g.allocation(occurrence=occ, producer=g.producer, product=p1, quantity=30, unit_price=500, unit="KG")
        g.allocation(occurrence=occ, producer=second_producer, product=p2, quantity=20, unit_price=400, unit="KG")
    conn.close()
    user, profile = _buyer(g)

    async def accept(session):
        return await _Svc(session, user, profile).accept_match_proposal(phone="+226", recurring_need_id=str(need), action="ACCEPT")

    result = _run(dsn, accept)
    return need, occ, result["order_ids"]


def _receive(dsn, g, order_id, outcome="RECEIVED"):
    _sql(dsn, "update marketplace.orders set delivery_status = 'DELIVERED' where id = %s and delivery_status in ('PENDING','IN_TRANSIT')", (order_id,))
    user, profile = _buyer(g)

    async def fn(session):
        return await _Svc(session, user, profile).record_order_reception(phone="+226", order_id=order_id, outcome=outcome)

    return _run(dsn, fn)


def _delivered(dsn, occ):
    return float(_sql(dsn, "select quantity_delivered from marketplace.recurring_need_occurrences where id = %s", (occ,))[0][0])


def test_quantity_delivered_is_the_exact_sum_of_received_orders_and_retry_safe(world):
    dsn, g = world
    _need, occ, orders = _accepted_occurrence(dsn, g, _fresh_day())
    assert len(orders) == 2  # one order per producer, same occurrence group
    assert _delivered(dsn, occ) == 0

    _sql(dsn, "update marketplace.orders set delivery_status = 'DELIVERED' where id = %s", (orders[0],))  # DELIVERED alone is not received
    assert _delivered(dsn, occ) == 0

    by_order = {oid: float(q) for oid, q in _sql(dsn, "select oi.order_id::text, sum(oi.quantity) from marketplace.order_items oi where oi.order_id = any(%s::uuid[]) group by 1", (orders,))}
    _receive(dsn, g, orders[0])
    first = _delivered(dsn, occ)
    assert first == by_order[orders[0]] and first in (30, 20)

    again = _receive(dsn, g, orders[0])  # replay of the same RECEIVED
    assert again["outcome"] == "ALREADY_RECORDED" and _delivered(dsn, occ) == first

    _receive(dsn, g, orders[1])
    assert _delivered(dsn, occ) == 50  # sum across producers, exact
    confirmed = float(_sql(dsn, "select quantity_confirmed from marketplace.recurring_need_occurrences where id = %s", (occ,))[0][0])
    assert _delivered(dsn, occ) <= confirmed


def test_received_with_issue_is_not_counted(world):
    dsn, g = world
    _need, occ, orders = _accepted_occurrence(dsn, g, _fresh_day())
    _receive(dsn, g, orders[0], outcome="RECEIVED_WITH_ISSUE")
    assert _delivered(dsn, occ) == 0


def test_aggregate_delivered_quantity_and_fulfillment_rate_and_recompute_stability(world):
    dsn, g = world
    day = _fresh_day()
    _need, occ, orders = _accepted_occurrence(dsn, g, day)
    _receive(dsn, g, orders[0])
    refresh = lambda: _run(dsn, lambda s: DailyMetricsRefresher(s).recompute_day(day))  # noqa: E731
    refresh()

    def row():
        r = _sql(dsn, "select delivered_quantity, confirmed_quantity from analytics.recurring_daily_metrics where metric_date = %s and zone_id = %s", (day, str(g.zone)))
        return [(float(a), float(b)) for a, b in r]

    first = row()
    refresh()
    assert row() == first  # same value on recompute
    delivered, confirmed = first[0]
    assert confirmed == 50 and delivered in (20, 30)

    async def metric(s):
        svc = AnalyticsService(s)
        return (await svc.get_metric("recurring_delivered_quantity", day, day, filters={"zone_id": g.zone}, compare=False),
                await svc.get_metric("recurring_fulfillment_rate", day, day, filters={"zone_id": g.zone}, compare=False))

    qty, rate = _run(dsn, metric)
    assert qty.numerator == delivered and qty.unit == "KG" and qty.status.value == "PARTIAL"
    assert (rate.numerator, rate.denominator) == (delivered, 50) and rate.value == pytest.approx(delivered / 50)

    _receive(dsn, g, orders[1])
    refresh()  # late reception updates the original cohort
    assert row()[0][0] == 50


def test_data_quality_detects_column_drift_and_backfill_fixes_it_idempotently(world):
    dsn, g = world
    day = _fresh_day()
    _need, occ, orders = _accepted_occurrence(dsn, g, day)
    # Simulate a RECEIVED that pre-dates the writer: flip the order state directly, column untouched.
    _sql(dsn, "update marketplace.orders set delivery_status = 'RECEIVED' where id = %s", (orders[0],))
    issues = _run(dsn, lambda s: run_data_quality_checks(s, day, day))
    assert any(i.check == "quantity_delivered_drift" for i in issues)
    fixed = _run(dsn, backfill_quantity_delivered)
    assert fixed >= 1
    assert _run(dsn, backfill_quantity_delivered) == 0
    issues = _run(dsn, lambda s: run_data_quality_checks(s, day, day))
    assert not any(i.check in ("quantity_delivered_drift", "delivered_gt_confirmed") for i in issues)


# ── DIRECT lifecycle ───────────────────────────────────────────────────────────────────────

def _outbox(dsn, event, order_id):
    return _sql(dsn, "select dedupe_key from analytics.event_outbox where event_name = %s and payload->>'entity_id' = %s", (event, str(order_id)))


def _pending_direct_order(dsn, g, **over):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        oid = g.order(zone_id=g.zone, order_type="PREORDER", status="PENDING_PRODUCER_CONFIRMATION", preorder_converted_at=datetime.utcnow(), **over)
        insert(cur, "marketplace.order_items", order_id=oid, product_id=g.product, quantity=2, price_at_sale=500)
    conn.close()
    return oid


def _producer_confirm(dsn, g, order_id):
    user = SimpleNamespace(id=g.producer_user)

    async def fn(session):
        return await _Svc(session, user, SimpleNamespace(id=g.producer)).confirm_order_by_producer(producer_phone="+226", order_id=str(order_id))

    return _run(dsn, fn)


def test_direct_order_confirmed_is_emitted_on_producer_acceptance_only_and_not_duplicated(world):
    dsn, g = world
    oid = _pending_direct_order(dsn, g)
    assert _outbox(dsn, "DIRECT_ORDER_CONFIRMED", oid) == []  # created/pending: not confirmed yet
    assert _producer_confirm(dsn, g, oid)["outcome"] == "CONFIRMED"
    assert _outbox(dsn, "DIRECT_ORDER_CONFIRMED", oid) == [(f"DIRECT_ORDER_CONFIRMED:{oid}",)]
    assert _producer_confirm(dsn, g, oid)["outcome"] == "ALREADY_CONFIRMED"  # retry
    assert len(_outbox(dsn, "DIRECT_ORDER_CONFIRMED", oid)) == 1


def test_recurring_and_tender_orders_never_emit_direct_order_confirmed(world):
    dsn, g = world
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        auction = g.auction()
        tender_order = g.order(zone_id=g.zone, auction_id=auction, status="PENDING_PRODUCER_CONFIRMATION")
        insert(cur, "marketplace.order_items", order_id=tender_order, product_id=g.product, quantity=1, price_at_sale=500)
        recurring_order = g.order(zone_id=g.zone, order_type="RECURRING_SUPPLY", status="PENDING_PRODUCER_CONFIRMATION")
        insert(cur, "marketplace.order_items", order_id=recurring_order, product_id=g.product, quantity=1, price_at_sale=500)
    conn.close()
    for oid in (tender_order, recurring_order):
        _producer_confirm(dsn, g, oid)
        assert _outbox(dsn, "DIRECT_ORDER_CONFIRMED", oid) == []


# ── DIRECT aggregates on the PREORDER checkout cohort ─────────────────────────────────────

def test_direct_aggregate_counts_converted_preorders_confirmed_and_delivered_but_not_drafts(world):
    dsn, g = world
    day = _fresh_day()
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        common = dict(zone_id=g.zone, order_type="PREORDER", total_amount=1000)
        # abandoned cart: DRAFT, never converted -> not a need
        g.order(status="DRAFT", created_at=_dt(day), **common)
        # converted, awaiting producer -> need, not confirmed
        g.order(status="PENDING_PRODUCER_CONFIRMATION", created_at=_dt(day - timedelta(days=3)), preorder_converted_at=_dt(day), **common)
        # converted + producer confirmed
        g.order(status="CONFIRMED", created_at=_dt(day - timedelta(days=3)), preorder_converted_at=_dt(day, 11), **common)
        # converted, confirmed, delivered
        g.order(status="COMPLETED", delivery_status="DELIVERED", created_at=_dt(day - timedelta(days=3)), preorder_converted_at=_dt(day, 12), **common)
        # confirmed (event) then cancelled -> still a confirmed fact
        cancelled = g.order(status="CANCELLED", created_at=_dt(day - timedelta(days=3)), preorder_converted_at=_dt(day, 13), **common)
        cur.execute(
            "insert into analytics.business_events (event_name, journey, actor_type, entity_type, entity_id, occurred_at, idempotency_key) "
            "values ('DIRECT_ORDER_CONFIRMED','DIRECT','PRODUCER','ORDER',%s, now(), %s)", (cancelled, f"DIRECT_ORDER_CONFIRMED:{cancelled}"))
    conn.close()
    _run(dsn, lambda s: DailyMetricsRefresher(s).recompute_day(day))
    r = _sql(dsn, "select orders_created, orders_confirmed, orders_delivered, confirmed_value from analytics.direct_daily_metrics where metric_date = %s and zone_id = %s", (day, str(g.zone)))
    assert [(a, b, c, float(d)) for a, b, c, d in r] == [(4, 3, 1, 3000.0)]

    async def rates(s):
        svc = AnalyticsService(s)
        return (await svc.get_metric("direct_fulfillment_rate", day, day, filters={"zone_id": g.zone}, compare=False),
                await svc.get_metric("direct_gmv", day, day, filters={"zone_id": g.zone}, compare=False))

    fulfillment, gmv = _run(dsn, rates)
    assert (fulfillment.numerator, fulfillment.denominator) == (1, 3) and fulfillment.value == pytest.approx(1 / 3)
    assert gmv.numerator == 3000
    issues = _run(dsn, lambda s: run_data_quality_checks(s, day, day))
    assert not any(i.check in ("delivered_gt_confirmed_orders", "missing_aggregate_days") and i.table.endswith("direct_daily_metrics") for i in issues)


def test_orders_per_search_is_a_window_ratio_not_attributed(world):
    dsn, g = world
    day = _fresh_day()
    _sql(dsn, "insert into analytics.direct_daily_metrics (metric_date, zone_id, searches, orders_created) values (%s, %s, 4, 6)", (day, str(g.zone)))
    res = _run(dsn, lambda s: AnalyticsService(s).get_metric("direct_orders_per_search", day, day, filters={"zone_id": g.zone}, compare=False))
    assert res.value == pytest.approx(1.5)  # > 1 is legitimate for a non-attributed window ratio
    assert any("NOT a conversion" in n for n in res.notes)
