"""B12 — cycle de vie d'une proposition recurring contre un VRAI PostgreSQL.

Prouve ce que les doubles ne peuvent pas prouver : (1) la garde de VERSION (accept ET reject), (2) deux
« oui » concurrents = UN seul effet (verrou `FOR UPDATE` réel), (3) le balayage no-response
(`expire_past_occurrences`) : occurrences passées -> EXPIRED, idempotent, et qui SAUTE une ligne verrouillée
par une réponse en cours (`SKIP LOCKED`). Même idiome que `test_recurring_need_confirmation_service.py`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.services.database.auction import AuctionMixin
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin):
    def __init__(self, session, user, profile):
        self._s, self._user, self._user_profile = session, user, profile

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._user, self._user_profile


@pytest.fixture
def market(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        user = SimpleNamespace(id=g.buyer_user, phone="+226", zone_id=None, name="Resto")
        profile = SimpleNamespace(id=g.buyer, establishment_name="Resto")
        tomorrow = datetime.utcnow() + timedelta(days=1)
        need = g.recurring_need(quantity=40, unit="KG", recurrence_type="DAILY")
        occ = g.occurrence(
            need, occurrence_date=tomorrow, requested_quantity=40, unit="KG", status="MATCHED",
            quantity_matched=40, notified_at=datetime.utcnow(),
        )
        product = g.product_for(quantity_for_sale=50)
        g.allocation(occurrence=occ, producer=g.producer, product=product, quantity=40, unit_price=500, unit="KG")
        # Besoin distinct, occurrence PASSÉE restée ouverte (aucune réponse) + une FUTURE intacte.
        past_need = g.recurring_need(quantity=10, unit="KG", recurrence_type="DAILY")
        past_occ = g.occurrence(
            past_need, occurrence_date=datetime.utcnow() - timedelta(days=3), requested_quantity=10, unit="KG",
            status="MATCHED", quantity_matched=10, notified_at=datetime.utcnow() - timedelta(days=4),
        )
        g.allocation(occurrence=past_occ, producer=g.producer, product=product, quantity=10, unit_price=500, unit="KG")
        future_occ = g.occurrence(
            past_need, occurrence_date=datetime.utcnow() + timedelta(days=2), requested_quantity=10, unit="KG",
            status="OPEN",
        )
    conn.close()
    return SimpleNamespace(dsn=pg_dsn, g=g, user=user, profile=profile, need=need, occ=occ, product=product,
                           past_need=past_need, past_occ=past_occ, future_occ=future_occ)


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


def _version(m) -> int:
    return int(_sql(m.dsn, "select version from marketplace.recurring_need_occurrences where id = %s", (str(m.occ),))[0][0])


def _occ_status(m, occ_id=None) -> str:
    return _sql(m.dsn, "select status from marketplace.recurring_need_occurrences where id = %s", (str(occ_id or m.occ),))[0][0]


def _stock(m) -> float:
    return float(_sql(m.dsn, "select quantity_for_sale from marketplace.products where id = %s", (str(m.product),))[0][0])


def _orders_for_group(m) -> int:
    return int(_sql(
        m.dsn,
        "select count(*) from marketplace.orders where order_type = 'RECURRING_SUPPLY' and buyer_id = %s",
        (str(m.g.buyer),),
    )[0][0])


def _accept(m, action="ACCEPT", *, version=None, occ=None):
    async def fn(session):
        return await _Svc(session, m.user, m.profile).accept_match_proposal(
            phone="+226", recurring_need_id=str(m.need), action=action,
            occurrence_id=str(occ or m.occ), expected_version=version,
        )

    return _run(m.dsn, fn)


# ── 1. VERSION ────────────────────────────────────────────────────────────────

def test_matching_version_accepts_normally_once(market):
    res = _accept(market, version=_version(market))
    assert res.get("outcome") is None and res["action"] == "ACCEPT" and len(res["order_ids"]) == 1
    assert _occ_status(market) == "ACCEPTED" and _stock(market) == 10.0
    assert _orders_for_group(market) == 1


def test_version_mismatch_accept_is_refused_with_zero_side_effect(market):
    stale = _version(market) - 1
    res = _accept(market, version=stale)
    assert res["outcome"] == "PROPOSAL_CHANGED"
    assert _occ_status(market) == "MATCHED" and _stock(market) == 50.0 and _orders_for_group(market) == 0
    alloc = _sql(market.dsn, "select status from marketplace.need_allocations where occurrence_id = %s", (str(market.occ),))
    assert [a[0] for a in alloc] == ["PROPOSED"]


def test_version_mismatch_reject_never_silently_rejects_the_new_version(market):
    res = _accept(market, action="REJECT", version=_version(market) + 3)
    assert res["outcome"] == "PROPOSAL_CHANGED"
    assert _occ_status(market) == "MATCHED"


def test_reject_with_matching_version_rejects_only_that_occurrence(market):
    res = _accept(market, action="REJECT", version=_version(market))
    assert res["action"] == "REJECT" and _occ_status(market) == "REJECTED"
    assert _orders_for_group(market) == 0 and _stock(market) == 50.0


# ── 2. CONCURRENCE : deux « oui » ─────────────────────────────────────────────

def test_two_concurrent_yes_produce_exactly_one_effective_accept(market):
    version = _version(market)

    async def one(engine):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            r = await _Svc(session, market.user, market.profile).accept_match_proposal(
                phone="+226", recurring_need_id=str(market.need), action="ACCEPT",
                occurrence_id=str(market.occ), expected_version=version,
            )
            await session.commit()
            return r

    async def go():
        engine = _engine(market.dsn)
        try:
            return await asyncio.gather(one(engine), one(engine))
        finally:
            await engine.dispose()

    results = asyncio.run(go())
    effective = [r for r in results if r.get("outcome") is None]
    refused = [r for r in results if r.get("outcome")]
    assert len(effective) == 1 and len(refused) == 1
    assert refused[0]["outcome"] == "ALREADY_PROCESSED"
    assert _orders_for_group(market) == 1 and _stock(market) == 10.0
    converted = _sql(market.dsn, "select count(*) from marketplace.need_allocations where occurrence_id = %s and status = 'CONVERTED'", (str(market.occ),))
    assert int(converted[0][0]) == 1


# ── 3. NO-RESPONSE : balayage d'expiration ────────────────────────────────────

def test_sweep_expires_past_occurrences_idempotently_and_never_touches_future_ones(market):
    async def sweep(session):
        return await _Svc(session, market.user, market.profile).expire_past_occurrences()

    first = _run(market.dsn, sweep)
    assert first["occurrences_expired"] >= 1
    assert _occ_status(market, market.past_occ) == "EXPIRED"
    assert _occ_status(market, market.future_occ) == "OPEN" and _occ_status(market) == "MATCHED"
    allocs = _sql(market.dsn, "select status from marketplace.need_allocations where occurrence_id = %s", (str(market.past_occ),))
    assert [a[0] for a in allocs] == ["EXPIRED"]
    version_after_first = int(_sql(market.dsn, "select version from marketplace.recurring_need_occurrences where id = %s", (str(market.past_occ),))[0][0])

    _run(market.dsn, sweep)  # rejeu : rien de plus, aucune version bumpée une 2e fois
    version_after_second = int(_sql(market.dsn, "select version from marketplace.recurring_need_occurrences where id = %s", (str(market.past_occ),))[0][0])
    assert version_after_first == version_after_second


def test_a_late_yes_on_an_expired_occurrence_creates_nothing(market):
    async def sweep(session):
        return await _Svc(session, market.user, market.profile).expire_past_occurrences()

    _run(market.dsn, sweep)

    async def fn(session):
        return await _Svc(session, market.user, market.profile).accept_match_proposal(
            phone="+226", recurring_need_id=str(market.past_need), action="ACCEPT",
            occurrence_id=str(market.past_occ), expected_version=1,
        )

    res = _run(market.dsn, fn)
    assert res["outcome"] == "EXPIRED"
    assert _orders_for_group(market) == 0 and _stock(market) == 50.0


def test_sweep_skips_a_row_locked_by_an_in_flight_reply(market):
    """Accept vs réconciliation : la réponse qui détient déjà `FOR UPDATE` GAGNE, le balayage saute la ligne
    (`SKIP LOCKED`) au lieu de l'expirer sous les pieds de la réponse ; une fois le verrou relâché, le
    balayage suivant l'expire."""
    holder = psycopg2.connect(market.dsn)
    try:
        with holder.cursor() as cur:
            cur.execute(
                "select id from marketplace.recurring_need_occurrences where id = %s for update",
                (str(market.past_occ),),
            )

        async def sweep(session):
            return await _Svc(session, market.user, market.profile).expire_past_occurrences()

        _run(market.dsn, sweep)
        assert _occ_status(market, market.past_occ) == "MATCHED"  # sautée : verrouillée
    finally:
        holder.rollback()
        holder.close()

    _run(market.dsn, sweep)
    assert _occ_status(market, market.past_occ) == "EXPIRED"
