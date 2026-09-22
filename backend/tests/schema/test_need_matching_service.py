"""`NeedMatchingService` contre un vrai PostgreSQL (même idiome que `test_recurring_supply_service.py`
et `test_query_efficiency.py`) : matching simple/multi-fournisseurs/stock insuffisant, filtres
(catégorie, unité, prix), statuts non éligibles jamais touchés, idempotence, rematch (expiration +
réactivation), historique jamais supprimé, concurrence (deux workers), stock jamais consommé, et
absence de N+1."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.domain.models import NeedAllocation, Product, RecurringNeedOccurrence
from ladini.workers.automation.need_matching_service import NeedMatchingService


def _run(dsn, fn):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _run_counted(dsn, fn):
    statements: list[str] = []

    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        event.listen(engine.sync_engine, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(go()), statements


@pytest.fixture
def market(pg_dsn):
    """Un besoin actif (100 kg, tomate) avec une occurrence OPEN due demain."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        cur.execute("update governance.sub_categories set name = %s where id = %s", ("tomate", g.sub_category))
        need = g.recurring_need(quantity=100, unit="KG", recurrence_type="DAILY")
        tomorrow = datetime.utcnow() + timedelta(days=1)
        occ = g.occurrence(need, occurrence_date=tomorrow, requested_quantity=100, unit="KG")
    conn.close()
    return pg_dsn, g, need, occ


async def _allocations(session, occurrence_id, *, active_only=True):
    stmt = select(NeedAllocation).where(NeedAllocation.occurrence_id == occurrence_id)
    if active_only:
        stmt = stmt.where(NeedAllocation.status == "PROPOSED")
    return (await session.execute(stmt)).scalars().all()


async def _occurrence(session, occurrence_id):
    return await session.get(RecurringNeedOccurrence, occurrence_id)


# ── matching simple ──────────────────────────────────────────────────────

def test_a_single_sufficient_offer_partially_covers_a_100kg_need(market):
    dsn, g, need, occ = market

    def seed():
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g2 = Graph.__new__(Graph)
            g2.cur = cur
            g2.sub_category, g2.producer = g.sub_category, g.producer
            g2.product_for(quantity_for_sale=60)
        conn.close()

    seed()

    async def fn(session):
        report = await NeedMatchingService(session).rematch_occurrence(occ)
        allocs = await _allocations(session, occ)
        occurrence = await _occurrence(session, occ)
        return report, allocs, occurrence

    report, allocs, occurrence = _run(dsn, fn)
    assert report.allocation_count == 1
    assert [a.quantity for a in allocs] == [60]
    assert occurrence.quantity_matched == 60
    assert occurrence.status == "OPEN"  # couverture partielle : reste ouvert (mandat §9)


# ── multi-fournisseurs ───────────────────────────────────────────────────

def test_three_offers_are_combined_to_exactly_cover_the_need(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        pB, pC = g.extra_producer(), g.extra_producer()
        g.product_for(producer=g.producer, quantity_for_sale=40, price=100)
        g.product_for(producer=pB, quantity_for_sale=35, price=100)
        g.product_for(producer=pC, quantity_for_sale=50, price=100)
    conn.close()

    async def fn(session):
        report = await NeedMatchingService(session).rematch_occurrence(occ)
        allocs = await _allocations(session, occ)
        occurrence = await _occurrence(session, occ)
        return report, allocs, occurrence

    report, allocs, occurrence = _run(dsn, fn)
    assert sum(a.quantity for a in allocs) == 100
    assert occurrence.quantity_matched == 100
    assert occurrence.status == "MATCHED"


# ── stock insuffisant ────────────────────────────────────────────────────

def test_insufficient_total_stock_keeps_the_occurrence_open(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        g.product_for(quantity_for_sale=60)
    conn.close()

    async def fn(session):
        await NeedMatchingService(session).rematch_occurrence(occ)
        return await _occurrence(session, occ)

    occurrence = _run(dsn, fn)
    assert occurrence.quantity_matched == 60
    assert occurrence.status == "OPEN"


# ── catégorie / unité / prix ─────────────────────────────────────────────

def test_a_different_sub_category_never_matches(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        other_cat = insert(cur, "governance.categories", name=uniq("cat"))
        other_sub = insert(cur, "governance.sub_categories", category_id=other_cat, name="oignon")
        g.product_for(sub_category_id=other_sub, quantity_for_sale=100)
    conn.close()

    async def fn(session):
        report = await NeedMatchingService(session).rematch_occurrence(occ)
        return report, await _occurrence(session, occ)

    report, occurrence = _run(dsn, fn)
    assert report.allocation_count == 0
    assert occurrence.quantity_matched == 0
    assert occurrence.status == "OPEN"


def test_a_price_above_the_cap_is_excluded(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=10, unit="KG", max_price_per_unit=500)
        occ = g.occurrence(need, requested_quantity=10, unit="KG")
        g.product_for(price=600, quantity_for_sale=100)
    conn.close()

    async def fn(session):
        await NeedMatchingService(session).rematch_occurrence(occ)
        return await _occurrence(session, occ)

    occurrence = _run(pg_dsn, fn)
    assert occurrence.quantity_matched == 0


def test_an_incompatible_unit_is_excluded(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        g.product_for(unit="UNITE", quantity_for_sale=100)
    conn.close()

    async def fn(session):
        await NeedMatchingService(session).rematch_occurrence(occ)
        return await _occurrence(session, occ)

    occurrence = _run(dsn, fn)
    assert occurrence.quantity_matched == 0


# ── besoin/occurrence non éligibles (mandat §18) ─────────────────────────

@pytest.mark.parametrize("need_status", ["PAUSED", "CANCELLED"])
def test_a_non_active_need_is_never_matched(pg_dsn, need_status):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=10, unit="KG", status=need_status)
        occ = g.occurrence(need, requested_quantity=10, unit="KG")
        g.product_for(quantity_for_sale=100)
    conn.close()

    async def fn(session):
        report = await NeedMatchingService(session).rematch_occurrence(occ)
        return report

    report = _run(pg_dsn, fn)
    assert report.allocation_count == 0
    assert report.skipped_reason is not None


@pytest.mark.parametrize(
    "occ_status", ["SKIPPED", "ACCEPTED", "PARTIALLY_ACCEPTED", "REJECTED", "EXPIRED", "FULFILLED", "PARTIALLY_FULFILLED", "UNFULFILLED", "CANCELLED"]
)
def test_a_non_matchable_occurrence_status_is_never_modified(pg_dsn, occ_status):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=10, unit="KG")
        occ = g.occurrence(need, requested_quantity=10, unit="KG", status=occ_status, quantity_matched=3)
        g.product_for(quantity_for_sale=100)
    conn.close()

    async def fn(session):
        await NeedMatchingService(session).rematch_occurrence(occ)
        return await _occurrence(session, occ)

    occurrence = _run(pg_dsn, fn)
    assert occurrence.status == occ_status  # jamais réécrit
    assert occurrence.quantity_matched == 3  # jamais recalculé
    assert occurrence.version == 1  # jamais bumpé


# ── idempotence / rematch / historique ───────────────────────────────────

def test_matching_twice_without_change_produces_the_same_allocations_and_no_version_bump(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        g.product_for(quantity_for_sale=60)
    conn.close()

    async def fn(session):
        svc = NeedMatchingService(session)
        r1 = await svc.rematch_occurrence(occ)
        v1 = (await _occurrence(session, occ)).version
        r2 = await svc.rematch_occurrence(occ)
        v2 = (await _occurrence(session, occ)).version
        allocs = await _allocations(session, occ)
        return r1, r2, v1, v2, allocs

    r1, r2, v1, v2, allocs = _run(dsn, fn)
    assert r1.changed is True
    assert r2.changed is False
    assert v1 == v2  # un rematch strictement identique ne bump jamais la version (mandat §10)
    assert len(allocs) == 1


def test_rematch_expires_a_disappeared_offer_and_proposes_a_new_one_bumping_the_version(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        prod_a = g.product_for(quantity_for_sale=100)
    conn.close()

    async def fn(session):
        svc = NeedMatchingService(session)
        await svc.rematch_occurrence(occ)
        v1 = (await _occurrence(session, occ)).version

        # Coop A retire son produit du catalogue ; Coop B publie.
        await session.execute(
            Product.__table__.update().where(Product.id == prod_a).values(is_available=False)
        )
        await session.commit()
        conn2 = psycopg2.connect(dsn)
        with conn2, conn2.cursor() as cur2:
            g.cur = cur2
            pB = g.extra_producer()
            g.product_for(producer=pB, quantity_for_sale=100)
        conn2.close()

        await svc.rematch_occurrence(occ)
        v2 = (await _occurrence(session, occ)).version
        all_allocs = await _allocations(session, occ, active_only=False)
        return v1, v2, all_allocs

    v1, v2, all_allocs = _run(dsn, fn)
    assert v2 == v1 + 1
    active = [a for a in all_allocs if a.status == "PROPOSED"]
    expired = [a for a in all_allocs if a.status == "EXPIRED"]
    assert len(active) == 1 and len(expired) == 1  # l'ancienne n'a jamais été supprimée (mandat §8)


def test_an_expired_allocation_can_be_reactivated_by_a_later_rematch(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        prod_a = g.product_for(quantity_for_sale=100)
    conn.close()

    async def fn(session):
        svc = NeedMatchingService(session)
        await svc.rematch_occurrence(occ)  # A proposé
        await session.execute(Product.__table__.update().where(Product.id == prod_a).values(is_available=False))
        await session.commit()
        await svc.rematch_occurrence(occ)  # A expiré (plus disponible)
        await session.execute(Product.__table__.update().where(Product.id == prod_a).values(is_available=True))
        await session.commit()
        await svc.rematch_occurrence(occ)  # A redevient disponible -> réactivé
        return await _allocations(session, occ, active_only=False)

    all_allocs = _run(dsn, fn)
    assert len(all_allocs) == 1  # même ligne réutilisée (UPSERT), pas une 2e créée
    assert all_allocs[0].status == "PROPOSED"


# ── stock jamais consommé (mandat §19) ────────────────────────────────────

def test_matching_never_decrements_quantity_for_sale(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        prod = g.product_for(quantity_for_sale=100)
    conn.close()

    async def fn(session):
        await NeedMatchingService(session).rematch_occurrence(occ)
        return await session.get(Product, prod)

    product = _run(dsn, fn)
    assert float(product.quantity_for_sale) == 100.0


# ── concurrence ───────────────────────────────────────────────────────────

def test_two_concurrent_workers_matching_the_same_occurrence_never_duplicate_allocations(market):
    dsn, g, need, occ = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        g.product_for(quantity_for_sale=60)
    conn.close()

    async def fn():
        engine1 = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        engine2 = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine1, expire_on_commit=False) as s1, AsyncSession(engine2, expire_on_commit=False) as s2:
                # FOR UPDATE sérialise : les deux appels sont lancés concurremment, PostgreSQL les ordonne.
                r1, r2 = await asyncio.gather(
                    NeedMatchingService(s1).rematch_occurrence(occ),
                    NeedMatchingService(s2).rematch_occurrence(occ),
                )
                allocs = (await s1.execute(select(NeedAllocation).where(NeedAllocation.occurrence_id == occ, NeedAllocation.status == "PROPOSED"))).scalars().all()
                occurrence = await s1.get(RecurringNeedOccurrence, occ)
                return r1, r2, allocs, occurrence
        finally:
            await engine1.dispose()
            await engine2.dispose()

    r1, r2, allocs, occurrence = asyncio.run(fn())
    assert len(allocs) == 1  # jamais dupliqué (UNIQUE + upsert)
    assert occurrence.quantity_matched == 60
    assert occurrence.version in (2, 3)  # 1 ou 2 des 2 tentatives a réellement changé l'état, jamais plus


# ── performance / N+1 (mandat §14/§20) ────────────────────────────────────

def test_matching_uses_a_bounded_number_of_queries_regardless_of_candidate_count(market):
    dsn, g, need, occ = market

    def seed(n):
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            for _ in range(n):
                g.product_for(producer=g.extra_producer(), quantity_for_sale=5)
        conn.close()

    seed(3)

    def match():
        async def fn(session):
            return await NeedMatchingService(session).rematch_occurrence(occ)

        return _run_counted(dsn, fn)

    _r_small, sql_small = match()

    seed(20)

    _r_big, sql_big = match()

    assert len(sql_big) == len(sql_small), f"N+1 : {len(sql_small)} requêtes pour peu de candidats, {len(sql_big)} pour plus"
