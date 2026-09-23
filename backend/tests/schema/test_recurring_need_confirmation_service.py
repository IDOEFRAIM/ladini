"""`RecurringSupplyMixin.accept_match_proposal` contre un vrai PostgreSQL (VS4 pilote) — même idiome
que `test_recurring_supply_service.py`/`test_need_matching_service.py` : accept simple, accept
multi-producteurs (une commande PAR producteur, corrélées), débit de stock exact, reject (aucune
commande, aucun débit), statut occurrence ACCEPTED/PARTIALLY_ACCEPTED selon la couverture, et
ownership (impossible d'agir sur le besoin d'un autre acheteur)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.domain.models import (
    NeedAllocation,
    Order,
    OrderItem,
    RecurringNeedOccurrence,
)
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin):
    """Même idiome que `test_recurring_supply_service.py::_Svc` — mixins branchés sur une session
    réelle, `get_buyer_profile` stubé (la session normalement fournie par `d.py`)."""

    def __init__(self, session, user):
        self._s = session
        self._user = user

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._user, self._user_profile


@pytest.fixture
def market(pg_dsn):
    """Acheteur, occurrence due demain avec ses allocations `PROPOSED` déjà posées (issues d'un
    matching déjà éprouvé par `test_need_matching_service.py` — ici on teste ce qui vient APRÈS,
    pas le matching lui-même)."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        buyer_user = SimpleNamespace(id=g.buyer_user)
        buyer_profile = SimpleNamespace(id=g.buyer)
        tomorrow = datetime.utcnow() + timedelta(days=1)
        need = g.recurring_need(quantity=40, unit="KG", recurrence_type="DAILY")
        occ = g.occurrence(need, occurrence_date=tomorrow, requested_quantity=40, unit="KG", status="MATCHED", quantity_matched=40)
        # `g.product` (créé par `Graph.__init__`) n'a PAS de `quantity_for_sale` explicite —
        # `product_for(quantity_for_sale=50)` en garantit un connu, nécessaire pour vérifier le débit.
        g.test_product = g.product_for(quantity_for_sale=50)
        g.allocation(occurrence=occ, producer=g.producer, product=g.test_product, quantity=40, unit_price=500, unit="KG")
    conn.close()
    return pg_dsn, g, buyer_user, buyer_profile, need, occ


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


def _svc(session, market_tuple) -> _Svc:
    _dsn, _g, user, profile, _need, _occ = market_tuple
    svc = _Svc(session, user)
    svc._user_profile = profile
    return svc


def _product_stock(dsn, product_id) -> float:
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute("select quantity_for_sale from marketplace.products where id = %s", (str(product_id),))
        row = cur.fetchone()
    conn.close()
    return float(row[0])


# ── ACCEPT — un seul fournisseur ─────────────────────────────────────────

def test_accepting_a_fully_matched_occurrence_creates_one_order_and_debits_stock(market):
    dsn, g, _user, _profile, _need, occ = market

    async def fn(session):
        return await _svc(session, market).accept_match_proposal(
            phone="+226", recurring_need_id=str(_need_id(market)), action="ACCEPT"
        )

    result = _run(dsn, fn)
    assert result["status"] == "success"
    assert len(result["order_ids"]) == 1
    assert result["quantity_confirmed"] == 40.0

    async def check(session):
        occurrence = await session.get(RecurringNeedOccurrence, occ)
        order = await session.get(Order, uuid.UUID(result["order_ids"][0]))
        items = (
            await session.execute(select(OrderItem).where(OrderItem.order_id == order.id))
        ).scalars().all()
        allocations = (
            await session.execute(select(NeedAllocation).where(NeedAllocation.occurrence_id == occ))
        ).scalars().all()
        return occurrence, order, items, allocations

    occurrence, order, items, allocations = _run(dsn, check)
    assert occurrence.status == "ACCEPTED"
    assert float(occurrence.quantity_confirmed) == 40.0
    assert occurrence.order_group_id is not None
    assert order.status == "PENDING_PRODUCER_CONFIRMATION"
    assert order.order_type == "RECURRING_SUPPLY"
    assert order.checkout_group_id == occurrence.order_group_id
    assert float(order.total_amount) == 40 * 500
    assert len(items) == 1 and float(items[0].quantity) == 40.0
    assert allocations[0].status == "CONVERTED"
    assert allocations[0].order_item_id == items[0].id

    assert _product_stock(dsn, g.test_product) == 10.0  # 50 - 40


# ── ACCEPT — plusieurs fournisseurs sur la MÊME occurrence ────────────────

def test_accepting_a_multi_producer_occurrence_creates_one_order_per_producer(market):
    dsn, g, _user, _profile, _need, occ = market
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        g.cur = cur
        producer_b = g.extra_producer()
        product_b = g.product_for(producer=producer_b, quantity_for_sale=30)
        g.allocation(occurrence=occ, producer=producer_b, product=product_b, quantity=5, unit_price=450, unit="KG")

    async def fn(session):
        return await _svc(session, market).accept_match_proposal(
            phone="+226", recurring_need_id=str(_need_id(market)), action="ACCEPT"
        )

    result = _run(dsn, fn)
    assert len(result["order_ids"]) == 2, "une commande par producteur, même occurrence"

    async def check(session):
        orders = [await session.get(Order, uuid.UUID(oid)) for oid in result["order_ids"]]
        return orders

    orders = _run(dsn, check)
    group_ids = {o.checkout_group_id for o in orders}
    assert len(group_ids) == 1, "les commandes d'une même occurrence partagent le même groupe"


# ── REJECT ──────────────────────────────────────────────────────────────

def test_rejecting_creates_no_order_and_debits_no_stock(market):
    dsn, g, _user, _profile, _need, occ = market

    async def fn(session):
        return await _svc(session, market).accept_match_proposal(
            phone="+226", recurring_need_id=str(_need_id(market)), action="REJECT"
        )

    result = _run(dsn, fn)
    assert result["status"] == "success"
    assert result["action"] == "REJECT"

    async def check(session):
        occurrence = await session.get(RecurringNeedOccurrence, occ)
        allocations = (
            await session.execute(select(NeedAllocation).where(NeedAllocation.occurrence_id == occ))
        ).scalars().all()
        # `pg_dsn` est session-scoped (une base partagée par tous les tests du module) : filtrer
        # par acheteur, jamais un `select(Order)` global qui verrait aussi les commandes des AUTRES
        # tests déjà exécutés dans la même base.
        orders = (
            await session.execute(select(Order).where(Order.buyer_id == _profile.id))
        ).scalars().all()
        return occurrence, allocations, orders

    occurrence, allocations, orders = _run(dsn, check)
    assert occurrence.status == "REJECTED"
    assert all(a.status == "REJECTED" for a in allocations)
    assert len(orders) == 0
    assert _product_stock(dsn, g.test_product) == 50.0  # jamais débité


# ── couverture partielle ──────────────────────────────────────────────────

def test_accepting_a_partial_match_leaves_the_occurrence_partially_accepted(market):
    dsn, g, _user, _profile, _need, _occ = market
    tomorrow = datetime.utcnow() + timedelta(days=1)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        g.cur = cur
        need2 = g.recurring_need(quantity=40, unit="KG", recurrence_type="DAILY")
        occ2 = g.occurrence(need2, occurrence_date=tomorrow, requested_quantity=40, unit="KG", quantity_matched=20)
        g.allocation(occurrence=occ2, producer=g.producer, product=g.test_product, quantity=20, unit_price=500, unit="KG")

    async def fn(session):
        return await _svc(session, market).accept_match_proposal(
            phone="+226", recurring_need_id=str(need2), action="ACCEPT"
        )

    result = _run(dsn, fn)
    assert result["quantity_confirmed"] == 20.0

    async def check(session):
        return await session.get(RecurringNeedOccurrence, occ2)

    occurrence = _run(dsn, check)
    assert occurrence.status == "PARTIALLY_ACCEPTED"


# ── protections ────────────────────────────────────────────────────────────

def test_no_pending_proposal_is_a_clean_business_error(market):
    dsn, g, _user, _profile, _need, occ = market
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "update marketplace.need_allocations set status = 'EXPIRED' where occurrence_id = %s",
            (str(occ),),
        )

    async def fn(session):
        return await _svc(session, market).accept_match_proposal(
            phone="+226", recurring_need_id=str(_need_id(market)), action="ACCEPT"
        )

    with pytest.raises(BusinessRuleException):
        _run(dsn, fn)


def test_accepting_someone_elses_need_is_refused(market):
    dsn, g, _user, _profile, _need, _occ = market
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        other_user = insert(cur, "auth.users", phone=uniq("+226"))
        other_buyer = insert(cur, "marketplace.buyer_profiles", user_id=other_user)

    async def fn(session):
        svc = _Svc(session, SimpleNamespace(id=other_user))
        svc._user_profile = SimpleNamespace(id=other_buyer)
        return await svc.accept_match_proposal(
            phone="+226", recurring_need_id=str(_need_id(market)), action="ACCEPT"
        )

    with pytest.raises(BusinessRuleException):
        _run(dsn, fn)


def test_an_unknown_action_is_refused_before_touching_anything(market):
    dsn, g, _user, _profile, _need, occ = market

    async def fn(session):
        return await _svc(session, market).accept_match_proposal(
            phone="+226", recurring_need_id=str(_need_id(market)), action="MAYBE"
        )

    with pytest.raises(BusinessRuleException):
        _run(dsn, fn)
    assert _product_stock(dsn, g.test_product) == 50.0


def _need_id(market_tuple):
    _dsn, _g, _user, _profile, need, _occ = market_tuple
    return need
