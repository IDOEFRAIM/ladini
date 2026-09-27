"""Real-PostgreSQL tests for `data_quality.run_supply_data_quality_checks`
(Producer Analytics Phase B, mission section 16) — the raw SUPPLY-side
checks over `marketplace.products` and `analytics.business_events`
directly, not over any daily aggregate (Phase B creates none). Self-skips
locally without SCHEMA_TEST_DSN, same discipline as every other file here."""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.services.analytics.data_quality import run_supply_data_quality_checks

_DAYS = iter(range(1, 2000))


def _fresh_day() -> date:
    return date(2022, 1, 1) + timedelta(days=next(_DAYS))


def _occurred_at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 10, tzinfo=timezone.utc)


def _run(dsn, fn):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def world(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
    conn.close()
    return pg_dsn, g


def _checks(dsn, day):
    return _run(dsn, lambda s: run_supply_data_quality_checks(s, day, day))


class TestSellableProductInvariants:
    def test_clean_catalog_raises_no_issue(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            g.product_for(quantity_for_sale=100, is_available=True)
        conn.close()
        issues = _checks(dsn, day)
        assert not any(i.check in ("sellable_product_negative_quantity", "available_product_not_actually_sellable") for i in issues)

    def test_available_with_negative_quantity_is_an_error(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            g.product_for(quantity_for_sale=-5, is_available=True)
        conn.close()
        issues = _checks(dsn, day)
        found = [i for i in issues if i.check == "sellable_product_negative_quantity"]
        assert found and found[0].severity == "ERROR"

    def test_available_with_zero_quantity_is_a_warning(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            g.product_for(quantity_for_sale=0, is_available=True)
        conn.close()
        issues = _checks(dsn, day)
        found = [i for i in issues if i.check == "available_product_not_actually_sellable"]
        assert found and found[0].severity == "WARNING"

    def test_paused_product_with_zero_quantity_raises_no_issue(self, world):
        """The exact post-fix shape of the old bug's aftermath: paused
        (is_available=False) products at 0 are normal, not a data-quality
        problem — the check only fires for products still marked AVAILABLE."""
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            g.product_for(quantity_for_sale=0, is_available=False)
        conn.close()
        issues = _checks(dsn, day)
        assert not any(i.check == "available_product_not_actually_sellable" for i in issues)


class TestSupplyEventProducerIdCoverage:
    def test_supply_event_with_producer_id_raises_no_issue(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            insert(
                cur, "analytics.business_events",
                event_name="PRODUCT_PUBLISHED_FOR_SALE", journey="SUPPLY", actor_type="PRODUCER",
                producer_id=g.producer, entity_type="PRODUCT", entity_id=g.product,
                occurred_at=_occurred_at(day), idempotency_key=uniq("idem"),
            )
        conn.close()
        issues = _checks(dsn, day)
        assert not any(i.check == "supply_event_missing_producer_id" for i in issues)

    def test_supply_event_without_producer_id_is_an_error(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            insert(
                cur, "analytics.business_events",
                event_name="PRODUCT_PUBLISHED_FOR_SALE", journey="SUPPLY", actor_type="PRODUCER",
                entity_type="PRODUCT", entity_id=g.product,
                occurred_at=_occurred_at(day), idempotency_key=uniq("idem"),
            )
        conn.close()
        issues = _checks(dsn, day)
        found = [i for i in issues if i.check == "supply_event_missing_producer_id"]
        assert found and found[0].severity == "ERROR"

    def test_direct_order_event_without_producer_id_is_a_warning(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            insert(
                cur, "analytics.business_events",
                event_name="DIRECT_ORDER_CREATED", journey="DIRECT", actor_type="BUYER",
                buyer_id=g.buyer, entity_type="ORDER", entity_id=uuid.uuid4(),
                occurred_at=_occurred_at(day), idempotency_key=uniq("idem"),
            )
        conn.close()
        issues = _checks(dsn, day)
        found = [i for i in issues if i.check == "direct_order_event_missing_producer_id"]
        assert found and found[0].severity == "WARNING"

    def test_direct_order_event_with_producer_id_raises_no_issue(self, world):
        dsn, g = world
        day = _fresh_day()
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            insert(
                cur, "analytics.business_events",
                event_name="DIRECT_ORDER_CREATED", journey="DIRECT", actor_type="BUYER",
                buyer_id=g.buyer, producer_id=g.producer, entity_type="ORDER", entity_id=uuid.uuid4(),
                occurred_at=_occurred_at(day), idempotency_key=uniq("idem"),
            )
        conn.close()
        issues = _checks(dsn, day)
        assert not any(i.check == "direct_order_event_missing_producer_id" for i in issues)
