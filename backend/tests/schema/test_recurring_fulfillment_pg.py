"""B14 — fermeture du cycle recurring contre un VRAI PostgreSQL (CI uniquement : `pg_dsn` saute sinon).

Chaque test appelle le code de production : accept exact, confirmation/annulation producteur, réception acheteur,
clôture cash B11, timeout producteur, filet de réconciliation, matching, digest, réapprovisionnement.
Dates 2031+ (`_fresh_date`) : la base est partagée par le fichier.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import psycopg2
from factories import Graph
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.services.database.auction import AuctionMixin
from ladini.services.database.buyer import BuyerMixin
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin
from ladini.workers.automation.need_matching_service import NeedMatchingService
from ladini.workers.automation.recurring_supply_digest_service import (
    RecurringSupplyDigestService,
)

_DAYS = iter(range(1, 5000))


def _fresh_date() -> date:
    return date(2031, 1, 1) + timedelta(days=next(_DAYS))


def _dt(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin, BuyerMixin):
    def __init__(self, session, buyer, producer):
        self._s, self._buyer, self._producer = session, buyer, producer

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._buyer

    async def get_producer_profile(self, phone):
        return self._producer


def _engine(dsn):
    return create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))


def _run(dsn, fn):
    async def go():
        engine = _engine(dsn)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _sql(dsn, query, params=()):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() if cur.description else []
    conn.close()
    return rows


def _ids(dsn, g, producer_id=None, producer_user=None):
    phone = _sql(dsn, "select phone from auth.users where id = %s", (str(g.buyer_user),))[0][0]
    buyer = (SimpleNamespace(id=g.buyer_user, phone=phone, zone_id=None, name="Resto"),
             SimpleNamespace(id=g.buyer, establishment_name="Resto"))
    producer = (SimpleNamespace(id=producer_user or g.producer_user), SimpleNamespace(id=producer_id or g.producer))
    return phone, buyer, producer


def _call(dsn, g, fn, *, producer=None, producer_user=None):
    phone, buyer, prod = _ids(dsn, g, producer, producer_user)

    async def go(session):
        return await fn(_Svc(session, buyer, prod), phone)

    return _run(dsn, go)


def _occ(dsn, occ):
    r = _sql(dsn, "select status, version, quantity_delivered, quantity_confirmed from marketplace.recurring_need_occurrences where id = %s", (str(occ),))[0]
    return {"status": r[0], "version": r[1], "delivered": float(r[2]), "confirmed": float(r[3])}


def _stock(dsn, product) -> float:
    return float(_sql(dsn, "select quantity_for_sale from marketplace.products where id = %s", (str(product),))[0][0])


def _seed(dsn, d, allocations, *, requested):
    """allocations : liste de (label, quantité, prix) ; un producteur + produit par ligne. Renvoie (g, need, occ, [(producer, user, product)])."""
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=requested, unit="KG", recurrence_type="DAILY", starts_at=_dt(d))
        occ = g.occurrence(need, occurrence_date=_dt(d), requested_quantity=requested, unit="KG", status="MATCHED",
                           quantity_matched=sum(a[1] for a in allocations), notified_at=datetime.utcnow())
        actors = []
        for i, (_label, qty, price) in enumerate(allocations):
            if i == 0:
                producer, puser = g.producer, g.producer_user
            else:
                producer = g.extra_producer()
                puser = _sql_user_of(cur, producer)
            product = g.product_for(producer=producer, quantity_for_sale=100)
            g.allocation(occurrence=occ, producer=producer, product=product, quantity=qty, unit_price=price, unit="KG")
            actors.append((producer, puser, product))
    conn.close()
    return g, need, occ, actors


def _sql_user_of(cur, producer):
    cur.execute("select user_id from marketplace.producers where id = %s", (str(producer),))
    return cur.fetchone()[0]


def _accept(dsn, g, need, occ):
    version = _occ(dsn, occ)["version"]

    async def fn(svc, phone):
        return await svc.accept_match_proposal(phone, str(need), "ACCEPT", occurrence_id=str(occ), expected_version=version)

    return _call(dsn, g, fn)["order_ids"]


def _order_of(dsn, order_ids, product):
    return [o for (o,) in _sql(dsn, "select distinct oi.order_id::text from marketplace.order_items oi where oi.order_id = any(%s::uuid[]) and oi.product_id = %s", (order_ids, str(product)))][0]


def _confirm(dsn, g, actor, order_id):
    producer, puser, _p = actor

    async def fn(svc, phone):
        return await svc.confirm_order_by_producer("+226", order_id)

    return _call(dsn, g, fn, producer=producer, producer_user=puser)


def _deliver_and_receive(dsn, g, actor, order_id):
    producer, puser, _p = actor

    async def deliver(svc, phone):
        await svc.mark_order_delivery_status("+226", order_id, "MARK_IN_TRANSIT")
        return await svc.mark_order_delivery_status("+226", order_id, "MARK_DELIVERED")

    _call(dsn, g, deliver, producer=producer, producer_user=puser)

    async def receive(svc, phone):
        return await svc.record_order_reception(phone, order_id, "RECEIVED")

    return _call(dsn, g, receive)


def _producer_cancel(dsn, g, actor, order_id):
    producer, puser, _p = actor

    async def fn(svc, phone):
        return await svc.cancel_confirmed_order("+226", order_id, "rupture")

    return _call(dsn, g, fn, producer=producer, producer_user=puser)


# ── 1. FULL ───────────────────────────────────────────────────────────────────

def test_full_fulfillment_single_order_then_next_occurrence_is_independent(pg_dsn):
    d = date.today() + timedelta(days=1)
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 20, 700)], requested=20)
    orders = _accept(pg_dsn, g, need, occ)
    assert _occ(pg_dsn, occ)["status"] == "ACCEPTED"
    _confirm(pg_dsn, g, actors[0], orders[0])
    _deliver_and_receive(pg_dsn, g, actors[0], orders[0])
    final = _occ(pg_dsn, occ)
    assert final["status"] == "FULFILLED" and final["delivered"] == 20.0
    assert _sql(pg_dsn, "select status from marketplace.recurring_needs where id = %s", (str(need),))[0][0] == "ACTIVE"

    async def view(svc, phone):
        return await svc.list_my_recurring_needs(phone)

    listed = [i for i in _call(pg_dsn, g, view)["items"] if i["recurring_need_id"] == str(need)][0]
    assert listed["next_occurrence_id"] != str(occ)  # une occurrence terminale n'est plus « actionnable »

    async def replenish(svc, phone):
        return await svc.replenish_occurrence_windows()

    _call(pg_dsn, g, replenish)
    nxt = _sql(pg_dsn, "select status, version, quantity_delivered, notified_at is not null from marketplace.recurring_need_occurrences "
                       "where recurring_need_id = %s and id <> %s and status = 'OPEN' order by occurrence_date asc limit 1", (str(need), str(occ)))
    assert nxt and nxt[0][0] == "OPEN" and nxt[0][1] == 1 and float(nxt[0][2]) == 0.0 and nxt[0][3] is False


# ── 2. MULTI-PRODUCTEUR : partiel, non terminal trop tôt, rejet + stock ───────────────────────────

def test_multi_producer_a_delivers_b_pending_then_b_rejects_gives_partial_and_restores_stock_once(pg_dsn):
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 60, 500), ("B", 40, 500)], requested=100)
    orders = _accept(pg_dsn, g, need, occ)
    oa, ob = _order_of(pg_dsn, orders, actors[0][2]), _order_of(pg_dsn, orders, actors[1][2])
    assert _stock(pg_dsn, actors[1][2]) == 60.0  # débité à l'acceptation
    _confirm(pg_dsn, g, actors[0], oa)
    _deliver_and_receive(pg_dsn, g, actors[0], oa)
    mid = _occ(pg_dsn, occ)
    assert mid["status"] == "ACCEPTED" and mid["delivered"] == 60.0  # B encore en cours : jamais terminal trop tôt

    assert _producer_cancel(pg_dsn, g, actors[1], ob)["outcome"] == "CANCELLED"
    final = _occ(pg_dsn, occ)
    assert final["status"] == "PARTIALLY_FULFILLED" and final["delivered"] == 60.0
    assert _stock(pg_dsn, actors[1][2]) == 100.0  # restitué
    assert _producer_cancel(pg_dsn, g, actors[1], ob)["outcome"] == "ALREADY_CANCELLED"
    assert _stock(pg_dsn, actors[1][2]) == 100.0  # PAS de double recrédit
    assert _occ(pg_dsn, occ)["version"] == final["version"]


def test_all_orders_rejected_is_unfulfilled_with_zero_delivered(pg_dsn):
    """B28 : une livraison dont la date est DÉJÀ passée (fenêtre fermée) ne se récupère pas : clôture honnête UNFULFILLED."""
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 60, 500), ("B", 40, 500)], requested=100)
    orders = _accept(pg_dsn, g, need, occ)
    _sql(pg_dsn, "update marketplace.recurring_need_occurrences set occurrence_date = %s where id = %s",
         (_dt(date.today() - timedelta(days=1)), str(occ)))
    for actor in actors:
        _producer_cancel(pg_dsn, g, actor, _order_of(pg_dsn, orders, actor[2]))
    final = _occ(pg_dsn, occ)
    assert final["status"] == "UNFULFILLED" and final["delivered"] == 0.0


def test_all_orders_rejected_while_the_window_is_open_reopens_the_occurrence_B28(pg_dsn):
    """B28 : l'échec des tentatives n'est pas l'échec de l'occurrence — livraison encore due : OPEN, historique conservé."""
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 60, 500), ("B", 40, 500)], requested=100)
    orders = _accept(pg_dsn, g, need, occ)
    for actor in actors:
        _producer_cancel(pg_dsn, g, actor, _order_of(pg_dsn, orders, actor[2]))
    final = _occ(pg_dsn, occ)
    assert final["status"] == "OPEN" and final["delivered"] == 0.0 and final["confirmed"] == 0.0
    assert [r[0] for r in _sql(pg_dsn, "select status from marketplace.need_allocations where occurrence_id = %s", (str(occ),))] == ["CONVERTED"] * 2


def test_a_buyer_cancellation_is_never_recovered(pg_dsn):
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 40, 500)], requested=40)
    order = _accept(pg_dsn, g, need, occ)[0]

    async def cancel(svc, phone):
        return await svc.cancel_pending_order(order, phone, "changé d avis")

    _call(pg_dsn, g, cancel)
    assert _occ(pg_dsn, occ)["status"] == "UNFULFILLED"


# ── 3. CLÔTURE CASH B11 sur une commande recurring ────────────────────────────────────────────────

def test_cash_closure_counts_as_received_fulfills_the_occurrence_and_replay_changes_nothing(pg_dsn):
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 40, 500)], requested=40)
    order = _accept(pg_dsn, g, need, occ)[0]

    async def close(svc, phone):
        await svc.confirm_order_by_producer("+226", order)
        return await svc.confirm_delivery_and_payment("+226", order)

    assert _call(pg_dsn, g, close)["outcome"] == "COMPLETED"
    row = _sql(pg_dsn, "select status, payment_status, delivery_status from marketplace.orders where id = %s", (order,))[0]
    assert row == ("COMPLETED", "PAID", "RECEIVED")
    final = _occ(pg_dsn, occ)
    assert final["status"] == "FULFILLED" and final["delivered"] == 40.0

    async def replay(svc, phone):
        return await svc.confirm_delivery_and_payment("+226", order)

    assert _call(pg_dsn, g, replay)["outcome"] == "ALREADY_COMPLETED"
    assert _occ(pg_dsn, occ) == final  # ni sur-comptage ni nouvelle version


# ── 4. CONCURRENCE : deux livraisons simultanées ──────────────────────────────────────────────────

def test_two_concurrent_receptions_never_lose_an_update(pg_dsn):
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 60, 500), ("B", 40, 500)], requested=100)
    orders = _accept(pg_dsn, g, need, occ)
    oa, ob = _order_of(pg_dsn, orders, actors[0][2]), _order_of(pg_dsn, orders, actors[1][2])
    for actor, order in ((actors[0], oa), (actors[1], ob)):
        _confirm(pg_dsn, g, actor, order)

        async def deliver(svc, phone, order=order):
            await svc.mark_order_delivery_status("+226", order, "MARK_IN_TRANSIT")
            return await svc.mark_order_delivery_status("+226", order, "MARK_DELIVERED")

        _call(pg_dsn, g, deliver, producer=actor[0], producer_user=actor[1])
    phone, buyer, prod = _ids(pg_dsn, g)

    async def one(engine, order):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            r = await _Svc(session, buyer, prod).record_order_reception(phone, order, "RECEIVED")
            await session.commit()
            return r

    async def go():
        engine = _engine(pg_dsn)
        try:
            return await asyncio.gather(one(engine, oa), one(engine, ob))
        finally:
            await engine.dispose()

    assert all(r["outcome"] == "RECORDED" for r in asyncio.run(go()))
    final = _occ(pg_dsn, occ)
    assert final["status"] == "FULFILLED" and final["delivered"] == 100.0


# ── 5. TIMEOUT PRODUCTEUR ─────────────────────────────────────────────────────────────────────────

def test_producer_never_confirms_timeout_cancels_restores_stock_once_and_closes_the_occurrence(pg_dsn):
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 40, 500)], requested=40)
    order = _accept(pg_dsn, g, need, occ)[0]
    assert _stock(pg_dsn, actors[0][2]) == 60.0
    _sql(pg_dsn, "update marketplace.orders set expected_fulfillment_date = %s where id = %s", (_dt(date.today() - timedelta(days=1)), order))
    # B28 : la date de livraison de l'occurrence est celle de la commande (ici passée : fenêtre de récupération fermée)
    _sql(pg_dsn, "update marketplace.recurring_need_occurrences set occurrence_date = %s where id = %s",
         (_dt(date.today() - timedelta(days=1)), str(occ)))

    async def sweep(svc, phone):
        return await svc.expire_unconfirmed_recurring_orders()

    assert _call(pg_dsn, g, sweep)["recurring_orders_expired"] >= 1
    assert _sql(pg_dsn, "select status, cancellation_role from marketplace.orders where id = %s", (order,))[0] == ("CANCELLED", "SYSTEM")
    assert _stock(pg_dsn, actors[0][2]) == 100.0
    assert _occ(pg_dsn, occ)["status"] == "UNFULFILLED"
    _call(pg_dsn, g, sweep)  # rejeu
    assert _stock(pg_dsn, actors[0][2]) == 100.0  # PAS de double recrédit


def test_a_confirmed_order_is_never_expired_by_the_timeout(pg_dsn):
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 40, 500)], requested=40)
    order = _accept(pg_dsn, g, need, occ)[0]
    _confirm(pg_dsn, g, actors[0], order)
    _sql(pg_dsn, "update marketplace.orders set expected_fulfillment_date = %s where id = %s", (_dt(date.today() - timedelta(days=1)), order))

    async def sweep(svc, phone):
        return await svc.expire_unconfirmed_recurring_orders()

    _call(pg_dsn, g, sweep)
    assert _sql(pg_dsn, "select status from marketplace.orders where id = %s", (order,))[0][0] == "CONFIRMED"
    assert _occ(pg_dsn, occ)["status"] == "ACCEPTED"


# ── 6. FILET DE RÉCONCILIATION (commande terminée hors chemin instrumenté / avant B14) ────────────

def test_reconciliation_heals_an_occurrence_whose_orders_were_completed_without_the_hook(pg_dsn):
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 40, 500)], requested=40)
    order = _accept(pg_dsn, g, need, occ)[0]
    _sql(pg_dsn, "update marketplace.orders set status = 'COMPLETED', delivery_status = 'RECEIVED' where id = %s", (order,))
    assert _occ(pg_dsn, occ)["status"] == "ACCEPTED"

    async def reconcile(svc, phone):
        return await svc.reconcile_recurring_fulfillment(limit=100000)

    _call(pg_dsn, g, reconcile)
    final = _occ(pg_dsn, occ)
    assert final["status"] == "FULFILLED" and final["delivered"] == 40.0


def test_reconciliation_concurrent_with_a_reception_never_errors_or_double_counts(pg_dsn):
    d = _fresh_date()
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 40, 500)], requested=40)
    order = _accept(pg_dsn, g, need, occ)[0]
    _confirm(pg_dsn, g, actors[0], order)

    async def deliver(svc, phone):
        await svc.mark_order_delivery_status("+226", order, "MARK_IN_TRANSIT")
        return await svc.mark_order_delivery_status("+226", order, "MARK_DELIVERED")

    _call(pg_dsn, g, deliver, producer=actors[0][0], producer_user=actors[0][1])
    phone, buyer, prod = _ids(pg_dsn, g)

    async def receive(engine):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            r = await _Svc(session, buyer, prod).record_order_reception(phone, order, "RECEIVED")
            await session.commit()
            return r

    async def reconcile(engine):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            r = await _Svc(session, buyer, prod).reconcile_recurring_fulfillment(limit=100000)
            await session.commit()
            return r

    async def go():
        engine = _engine(pg_dsn)
        try:
            return await asyncio.gather(receive(engine), reconcile(engine))
        finally:
            await engine.dispose()

    asyncio.run(go())
    final = _occ(pg_dsn, occ)
    assert final["status"] == "FULFILLED" and final["delivered"] == 40.0


# ── 7. OCCURRENCE TERMINALE : digest, matching, accept tardif ─────────────────────────────────────

def test_a_terminal_occurrence_is_excluded_from_digest_matching_and_late_yes(pg_dsn):
    d = date.today() + timedelta(days=1)
    g, need, occ, actors = _seed(pg_dsn, d, [("A", 40, 500)], requested=40)
    order = _accept(pg_dsn, g, need, occ)[0]
    _confirm(pg_dsn, g, actors[0], order)
    _deliver_and_receive(pg_dsn, g, actors[0], order)
    assert _occ(pg_dsn, occ)["status"] == "FULFILLED"

    async def digest(session):
        await RecurringSupplyDigestService(session).run(target_date=d)
        return None

    _run(pg_dsn, digest)
    mine = _sql(pg_dsn, "select count(*) from intelligence.notification_outbox where template_key = 'RECURRING_SUPPLY_DIGEST_BUYER' and dedupe_key like %s",
                (f"recurring_supply_digest:{g.buyer}:%",))
    assert int(mine[0][0]) == 0

    before = _occ(pg_dsn, occ)
    _run(pg_dsn, lambda s: NeedMatchingService(s).rematch_occurrence(occ))
    assert _occ(pg_dsn, occ) == before
    assert int(_sql(pg_dsn, "select count(*) from marketplace.need_allocations where occurrence_id = %s and status = 'PROPOSED'", (str(occ),))[0][0]) == 0

    async def late_yes(svc, phone):
        return await svc.accept_match_proposal(phone, str(need), "ACCEPT", occurrence_id=str(occ), expected_version=before["version"])

    assert _call(pg_dsn, g, late_yes)["outcome"] == "ALREADY_PROCESSED"
    assert int(_sql(pg_dsn, "select count(*) from marketplace.orders where buyer_id = %s and order_type = 'RECURRING_SUPPLY'", (str(g.buyer),))[0][0]) == 1
