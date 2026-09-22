"""`RecurringSupplyDigestService` contre un vrai PostgreSQL (même idiome que
`test_need_matching_service.py`) : un digest par (acheteur, date) jamais par besoin, deux
restaurants → deux digests distincts, idempotence, changement de version → nouvelle dedupe key,
retry sans doublon, aucune commande/stock touché, absence de N+1.

Chaque test utilise sa PROPRE date cible (`_fresh_date()`) : `pg_dsn` est une base PARTAGÉE par tout
le fichier (fixture session-scope, même convention que `test_need_matching_service.py`) — sans une
date distincte par test, les lignes d'outbox d'un test contamineraient les assertions `== []`/
`len(rows) == N` d'un autre (le digest ne filtre QUE par date, pas par un marqueur de test)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.domain.models import NotificationOutbox, Product, RecurringNeedOccurrence
from ladini.workers.automation.recurring_supply_digest_service import (
    RecurringSupplyDigestService,
)

_DATE_COUNTER = iter(range(1, 1000))


def _fresh_date() -> date:
    return date(2027, 1, 1) + timedelta(days=next(_DATE_COUNTER))


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


def _seed_buyer_with_needs(dsn, *, products, occurrence_date: date):
    """Un acheteur avec N besoins actifs, chacun avec une occurrence OPEN/MATCHED à `occurrence_date`."""
    occurrence_dt = datetime.combine(occurrence_date, datetime.min.time())
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        occ_ids = []
        for label, requested, matched in products:
            cat = insert(cur, "governance.categories", name=uniq("cat"))
            sub = insert(cur, "governance.sub_categories", category_id=cat, name=label)
            need = g.recurring_need(quantity=requested, unit="KG", sub_category_id=sub)
            occ = g.occurrence(
                need, occurrence_date=occurrence_dt, requested_quantity=requested, unit="KG",
                quantity_matched=matched, status="MATCHED" if matched >= requested else "OPEN",
            )
            occ_ids.append(occ)
    conn.close()
    return g, occ_ids


async def _outbox_rows_for(session, d: date):
    rows = (
        await session.execute(select(NotificationOutbox).where(NotificationOutbox.template_key == "RECURRING_SUPPLY_DIGEST_BUYER"))
    ).scalars().all()
    return [r for r in rows if f":{d.isoformat()}:" in r.dedupe_key]


# ── un digest par acheteur, jamais un par besoin (mandat §2/§21) ─────────

def test_a_buyer_with_four_needs_gets_exactly_one_notification(pg_dsn):
    d = _fresh_date()
    _seed_buyer_with_needs(pg_dsn, occurrence_date=d, products=[("tomate", 40, 40), ("oignon", 20, 20), ("pdt", 50, 30), ("poulet", 30, 0)])

    async def fn(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        return await _outbox_rows_for(session, d)

    rows = _run(pg_dsn, fn)
    assert len(rows) == 1
    body = rows[0].payload["body"]
    assert all(p in body for p in ("Tomate", "Oignon", "Pdt", "Poulet"))
    assert "1. Voir les détails" in body


def test_the_digest_never_promises_a_reservation(pg_dsn):
    d = _fresh_date()
    _seed_buyer_with_needs(pg_dsn, occurrence_date=d, products=[("tomate", 40, 40)])

    async def fn(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        return await _outbox_rows_for(session, d)

    rows = _run(pg_dsn, fn)
    body = rows[0].payload["body"].lower()
    assert not any(w in body for w in ("réservé", "garanti", "commande confirmée"))


# ── deux restaurants → deux digests distincts ────────────────────────────

def test_two_restaurants_get_two_distinct_digests(pg_dsn):
    d = _fresh_date()
    d_dt = datetime.combine(d, datetime.min.time())
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        gA = Graph(cur)
        needA = gA.recurring_need(quantity=40, unit="KG")
        gA.occurrence(needA, occurrence_date=d_dt, requested_quantity=40, unit="KG", quantity_matched=40, status="MATCHED")

        uB = insert(cur, "auth.users", phone=uniq("+226"))
        buyerB = insert(cur, "marketplace.buyer_profiles", user_id=uB)
        gA.buyer, gA.buyer_user = buyerB, uB
        needB = gA.recurring_need(quantity=20, unit="KG")
        gA.occurrence(needB, occurrence_date=d_dt, requested_quantity=20, unit="KG", quantity_matched=10, status="OPEN")
    conn.close()

    async def fn(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        return await _outbox_rows_for(session, d)

    rows = _run(pg_dsn, fn)
    assert len(rows) == 2
    assert len({r.recipient_phone for r in rows}) == 2  # deux numéros distincts, un par acheteur
    assert len({r.dedupe_key for r in rows}) == 2


# ── idempotence / retry (mandat §9/§21) ──────────────────────────────────

def test_running_the_job_twice_without_change_sends_only_one_notification(pg_dsn):
    d = _fresh_date()
    _seed_buyer_with_needs(pg_dsn, occurrence_date=d, products=[("tomate", 40, 40), ("oignon", 20, 20)])

    async def fn(session):
        svc = RecurringSupplyDigestService(session)
        b1 = await svc.run(target_date=d)
        b2 = await svc.run(target_date=d)
        rows = await _outbox_rows_for(session, d)
        return b1, b2, rows

    b1, b2, rows = _run(pg_dsn, fn)
    assert len(rows) == 1
    assert b1.reports[0].enqueued is True
    assert b2.reports[0].enqueued is False and b2.reports[0].deduplicated is True


def test_a_retried_task_never_duplicates_the_notification(pg_dsn):
    """Même scénario que l'idempotence mais formulé comme un retry Celery : la tâche est rejouée par
    un NOUVEAU worker/session, jamais le même objet Python — donc un vrai round-trip DB à chaque fois."""
    d = _fresh_date()
    _seed_buyer_with_needs(pg_dsn, occurrence_date=d, products=[("tomate", 40, 40)])

    def attempt():
        async def fn(session):
            return await RecurringSupplyDigestService(session).run(target_date=d)

        return _run(pg_dsn, fn)

    attempt()
    attempt()
    attempt()

    async def count(session):
        return await _outbox_rows_for(session, d)

    rows = _run(pg_dsn, count)
    assert len(rows) == 1


# ── changement de proposition → nouvelle dedupe key (mandat §10/§21) ─────

def test_a_version_change_after_a_first_digest_triggers_a_new_notification(pg_dsn):
    d = _fresh_date()
    g, occ_ids = _seed_buyer_with_needs(pg_dsn, occurrence_date=d, products=[("tomate", 40, 20)])
    occ_id = occ_ids[0]

    async def fn(session):
        svc = RecurringSupplyDigestService(session)
        await svc.run(target_date=d)

        # Le matching (Phase 3) recompose l'allocation : quantity_matched et version changent.
        occ = await session.get(RecurringNeedOccurrence, occ_id)
        occ.quantity_matched = 40
        occ.version = occ.version + 1
        await session.commit()

        await svc.run(target_date=d)
        return await _outbox_rows_for(session, d)

    rows = _run(pg_dsn, fn)
    assert len(rows) == 2
    assert rows[0].dedupe_key != rows[1].dedupe_key


# ── notified_at (mandat §11) ──────────────────────────────────────────────

def test_notified_at_is_set_on_enqueue_and_not_advanced_by_a_deduplicated_rerun(pg_dsn):
    d = _fresh_date()
    g, occ_ids = _seed_buyer_with_needs(pg_dsn, occurrence_date=d, products=[("tomate", 40, 40)])
    occ_id = occ_ids[0]

    async def fn(session):
        svc = RecurringSupplyDigestService(session)
        await svc.run(target_date=d)
        first = (await session.get(RecurringNeedOccurrence, occ_id)).notified_at

        await svc.run(target_date=d)  # dédupliqué : ne doit rien avancer
        second = (await session.get(RecurringNeedOccurrence, occ_id)).notified_at
        return first, second

    first, second = _run(pg_dsn, fn)
    assert first is not None
    assert first == second


# ── aucune commande, aucun stock touché (mandat §12/§13/§19) ─────────────

def test_no_order_is_created_and_no_stock_is_touched(pg_dsn):
    d = _fresh_date()
    d_dt = datetime.combine(d, datetime.min.time())
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        prod = g.product_for(quantity_for_sale=100)
        need = g.recurring_need(quantity=40, unit="KG")
        g.occurrence(need, occurrence_date=d_dt, requested_quantity=40, unit="KG", quantity_matched=40, status="MATCHED")
    conn.close()

    async def fn(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        from sqlalchemy import text

        orders = (await session.execute(text("SELECT count(*) FROM marketplace.orders"))).scalar()
        order_items = (await session.execute(text("SELECT count(*) FROM marketplace.order_items"))).scalar()
        product = await session.get(Product, prod)
        return orders, order_items, product

    orders, order_items, product = _run(pg_dsn, fn)
    assert orders == 0 and order_items == 0
    assert float(product.quantity_for_sale) == 100.0


# ── occurrence status jamais réécrit (mandat §12, décision Option A) ─────

def test_the_occurrence_status_is_never_rewritten_to_proposed(pg_dsn):
    d = _fresh_date()
    g, occ_ids = _seed_buyer_with_needs(pg_dsn, occurrence_date=d, products=[("tomate", 40, 40)])
    occ_id = occ_ids[0]

    async def fn(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        return await session.get(RecurringNeedOccurrence, occ_id)

    occurrence = _run(pg_dsn, fn)
    assert occurrence.status == "MATCHED"  # jamais "PROPOSED"


# ── exclusions (mandat §3) ────────────────────────────────────────────────

@pytest.mark.parametrize("excluded_status", ["SKIPPED", "CANCELLED", "REJECTED", "EXPIRED", "FULFILLED", "PARTIALLY_FULFILLED", "UNFULFILLED"])
def test_non_relevant_occurrence_statuses_are_excluded_from_the_digest(pg_dsn, excluded_status):
    d = _fresh_date()
    d_dt = datetime.combine(d, datetime.min.time())
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=40, unit="KG")
        g.occurrence(need, occurrence_date=d_dt, requested_quantity=40, unit="KG", status=excluded_status)
    conn.close()

    async def fn(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        return await _outbox_rows_for(session, d)

    assert _run(pg_dsn, fn) == []


def test_a_paused_needs_occurrence_is_excluded(pg_dsn):
    d = _fresh_date()
    d_dt = datetime.combine(d, datetime.min.time())
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=40, unit="KG", status="PAUSED")
        g.occurrence(need, occurrence_date=d_dt, requested_quantity=40, unit="KG", status="OPEN")
    conn.close()

    async def fn(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        return await _outbox_rows_for(session, d)

    assert _run(pg_dsn, fn) == []


# ── N+1 (mandat §22) ───────────────────────────────────────────────────────

def test_digest_generation_uses_a_bounded_number_of_queries_regardless_of_need_count(pg_dsn):
    def seed(n, occurrence_date):
        occurrence_dt = datetime.combine(occurrence_date, datetime.min.time())
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            g = Graph(cur)
            u = insert(cur, "auth.users", phone=uniq("+226"))
            buyer = insert(cur, "marketplace.buyer_profiles", user_id=u)
            g.buyer, g.buyer_user = buyer, u
            for i in range(n):
                cat = insert(cur, "governance.categories", name=uniq("cat"))
                sub = insert(cur, "governance.sub_categories", category_id=cat, name=f"produit-{i}-{uniq('x')}")
                need = g.recurring_need(quantity=10, unit="KG", sub_category_id=sub)
                g.occurrence(need, occurrence_date=occurrence_dt, requested_quantity=10, unit="KG", quantity_matched=5, status="OPEN")
        conn.close()

    def run_digest(target_date):
        async def fn(session):
            return await RecurringSupplyDigestService(session).run(target_date=target_date)

        return _run_counted(pg_dsn, fn)

    small_date, big_date = _fresh_date(), _fresh_date()
    seed(3, small_date)
    _r_small, sql_small = run_digest(small_date)

    seed(20, big_date)
    _r_big, sql_big = run_digest(big_date)

    assert len(sql_big) <= len(sql_small) + 2, f"N+1 : {len(sql_small)} requêtes pour peu de besoins, {len(sql_big)} pour plus"
