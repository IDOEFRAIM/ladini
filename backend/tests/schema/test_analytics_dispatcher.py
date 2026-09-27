"""`AnalyticsEventDispatcher` contre un vrai PostgreSQL (CI) : outbox PENDING -> business_events
réel (incluant la colonne JSONB `metadata`), rejeu sans doublon, et collision de clé d'idempotence
explicite. Le `worker_session` est rebranché sur le moteur de test (aucun `init_db` global)."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import psycopg2
import pytest
from factories import Graph, uniq
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.domain.analytics.business_events import BusinessEventName
from ladini.domain.analytics.emitter import BusinessEventEmitter
from ladini.domain.analytics.metric_dictionary import Journey


def _drain(dsn, monkeypatch):
    from ladini.workers.outbox import analytics_dispatcher as mod

    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))

        @asynccontextmanager
        async def _ws():
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield session
                await session.commit()

        monkeypatch.setattr(mod, "worker_session", _ws)
        try:
            return await mod.AnalyticsEventDispatcher().run()
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _enqueue(dsn, *, key, entity_id, buyer_id):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                inserted = await BusinessEventEmitter(session).emit(
                    event_name=BusinessEventName.RECURRING_NEED_CREATED,
                    journey=Journey.RECURRING,
                    actor_type="BUYER",
                    buyer_id=buyer_id,
                    entity_type="RECURRING_NEED",
                    entity_id=entity_id,
                    idempotency_key=key,
                    quantity=1500,
                    unit="G",
                    canonical_unit_override="KG",
                    metadata={"recurrence_type": "DAILY"},
                )
                await session.commit()
                return inserted
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture
def buyer(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        buyer_id = Graph(cur).buyer
    conn.close()
    return buyer_id


def _rows(dsn, key):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(
            "select canonical_quantity, canonical_unit, metadata->>'recurrence_type' "
            "from analytics.business_events where idempotency_key = %s",
            (key,),
        )
        rows = cur.fetchall()
        cur.execute("select status from analytics.event_outbox where dedupe_key = %s", (key,))
        status = cur.fetchall()
    conn.close()
    return rows, status


def test_pending_outbox_item_lands_in_business_events_and_is_marked_sent(pg_dsn, buyer, monkeypatch):
    key = uniq("RECURRING_NEED_CREATED")
    assert _enqueue(pg_dsn, key=key, entity_id=uuid.uuid4(), buyer_id=buyer) is True
    _drain(pg_dsn, monkeypatch)
    rows, status = _rows(pg_dsn, key)
    assert len(rows) == 1 and float(rows[0][0]) == 1.5 and rows[0][1:] == ("KG", "DAILY")
    assert status == [("SENT",)]


def test_second_drain_and_same_key_enqueue_never_duplicate_the_business_event(pg_dsn, buyer, monkeypatch):
    key = uniq("RECURRING_NEED_CREATED")
    entity = uuid.uuid4()
    _enqueue(pg_dsn, key=key, entity_id=entity, buyer_id=buyer)
    _drain(pg_dsn, monkeypatch)
    assert _enqueue(pg_dsn, key=key, entity_id=entity, buyer_id=buyer) is False  # collision => déduplication explicite
    _drain(pg_dsn, monkeypatch)
    rows, status = _rows(pg_dsn, key)
    assert len(rows) == 1 and status == [("SENT",)]
