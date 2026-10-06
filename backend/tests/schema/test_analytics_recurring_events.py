"""Phase C — RECURRING business events contre un vrai PostgreSQL (exécutés en CI ; aucun DSN local).

Chaque test vérifie l'INTENTION d'event dans `analytics.event_outbox` : écrite dans la MÊME
transaction que le fait métier, dédupliquée par clé d'idempotence sur le FAIT (jamais sur le
message WhatsApp/Celery). Les IDs d'entité sont uniques par test (base partagée, session-scope)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph
from observed import observed_update
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.services.database.recurring_supply as recurring_supply_module
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin
from ladini.workers.automation.need_matching_service import NeedMatchingService
from ladini.workers.automation.recurring_supply_digest_service import (
    RecurringSupplyDigestService,
)

FIXED_TODAY = date(2026, 9, 15)


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin):
    def __init__(self, session, user, profile):
        self._s = session
        self._user = user
        self._user_profile = profile

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._user, self._user_profile


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(recurring_supply_module, "_today", lambda: FIXED_TODAY)


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


@pytest.fixture
def market(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        cur.execute("update governance.sub_categories set name = %s where id = %s", ("tomate", g.sub_category))
        user = SimpleNamespace(id=g.buyer_user)
        profile = SimpleNamespace(id=g.buyer)
    conn.close()
    return pg_dsn, g, user, profile


async def _events(session, event_name, *, entity_id=None, buyer_id=None):
    rows = (
        await session.execute(
            text("select dedupe_key, payload from analytics.event_outbox where event_name = :n"),
            {"n": event_name},
        )
    ).all()
    out = []
    for key, payload in rows:
        if entity_id is not None and payload.get("entity_id") != str(entity_id):
            continue
        if buyer_id is not None and payload.get("buyer_id") != str(buyer_id):
            continue
        out.append((key, payload))
    return out


def _create(market):
    dsn, _g, user, profile = market

    async def fn(session):
        return await _Svc(session, user, profile).create_recurring_need(
            phone="+226", product_query="tomate", quantity=40, unit="KG", recurrence_type="DAILY"
        )

    return _run(dsn, fn)


# A + B — création du besoin et de ses occurrences initiales
def test_create_need_emits_need_created_and_one_event_per_initial_occurrence(market):
    dsn, _g, _u, profile = market
    result = _create(market)
    need_id = result["recurring_need_id"]

    async def fn(session):
        needs = await _events(session, "RECURRING_NEED_CREATED", entity_id=need_id)
        occs = [
            e
            for e in await _events(session, "RECURRING_OCCURRENCE_CREATED", buyer_id=profile.id)
            if e[1]["metadata"]["recurring_need_id"] == need_id
        ]
        return needs, occs

    needs, occs = _run(dsn, fn)
    assert len(needs) == 1
    assert needs[0][0] == f"RECURRING_NEED_CREATED:{need_id}"
    assert needs[0][1]["journey"] == "RECURRING" and needs[0][1]["actor_type"] == "BUYER"
    assert len(occs) == result["occurrences_created"] == 4  # 19..22 : aujourd'hui + délai minimal par défaut (4)


# C — le réapprovisionnement passe par le MÊME point d'émission, et un rejeu n'émet rien
def test_replenishment_emits_same_event_and_replay_emits_nothing(market, monkeypatch):
    dsn, _g, _u, profile = market
    result = _create(market)
    need_id = result["recurring_need_id"]
    monkeypatch.setattr(recurring_supply_module, "_today", lambda: FIXED_TODAY + timedelta(days=3))

    async def replenish(session):
        return await _Svc(session, None, None).replenish_occurrence_windows()

    first = _run(dsn, replenish)
    second = _run(dsn, replenish)

    async def count(session):
        events = [
            e
            for e in await _events(session, "RECURRING_OCCURRENCE_CREATED", buyer_id=profile.id)
            if e[1]["metadata"]["recurring_need_id"] == need_id
        ]
        rows = (
            await session.execute(
                text("select count(*) from marketplace.recurring_need_occurrences where recurring_need_id = :n"),
                {"n": need_id},
            )
        ).scalar()
        return len(events), rows

    assert first["occurrences_created"] >= 1
    assert second["occurrences_created"] == 0
    n_events, n_rows = _run(dsn, count)
    assert n_events == n_rows and n_rows > 4  # 1 event par occurrence RÉELLEMENT insérée, ni plus ni moins


# D + E — allocation matérialisée ; rematch identique => aucun doublon
def test_match_found_is_one_event_per_allocation_and_rematch_does_not_duplicate(market):
    dsn, g, _u, profile = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        need = g.recurring_need(quantity=100, unit="KG", recurrence_type="DAILY")
        occ = g.occurrence(need, occurrence_date=datetime.utcnow() + timedelta(days=1), requested_quantity=100, unit="KG")
        g.product_for(quantity_for_sale=60, price=100)
    conn.close()

    async def rematch(session):
        return await NeedMatchingService(session).rematch_occurrence(occ)

    _run(dsn, rematch)
    _run(dsn, rematch)

    async def fn(session):
        allocs = (
            await session.execute(text("select id from marketplace.need_allocations where occurrence_id = :o"), {"o": occ})
        ).all()
        events = [
            e
            for e in await _events(session, "RECURRING_MATCH_FOUND", buyer_id=profile.id)
            if e[1]["metadata"]["occurrence_id"] == str(occ)
        ]
        return allocs, events

    allocs, events = _run(dsn, fn)
    assert len(allocs) == 1
    assert len(events) == 1
    assert events[0][0] == f"RECURRING_MATCH_FOUND:{allocs[0][0]}"


# F — digest mis en file
def test_digest_queued_emits_digest_sent_and_cron_replay_does_not_duplicate(market):
    dsn, g, _u, profile = market
    target = date(2028, 3, 1) + timedelta(days=uuid.uuid4().int % 300)
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        need = g.recurring_need(quantity=40, unit="KG", recurrence_type="DAILY")
        g.occurrence(need, occurrence_date=datetime.combine(target, datetime.min.time()), requested_quantity=40, unit="KG", quantity_matched=40, status="MATCHED")
    conn.close()

    async def digest(session):
        return await RecurringSupplyDigestService(session).run(target_date=target)

    _run(dsn, digest)
    _run(dsn, digest)

    async def fn(session):
        return [
            e
            for e in await _events(session, "RECURRING_DIGEST_SENT", buyer_id=profile.id)
            if e[1]["metadata"]["occurrence_date"] == target.isoformat()
        ]

    events = _run(dsn, fn)
    assert len(events) == 1
    assert events[0][1]["metadata"]["delivery_guarantee"] == "QUEUED_FOR_OUTBOUND_DELIVERY"


def _matched_occurrence(market):
    dsn, g, _u, _p = market
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        need = g.recurring_need(quantity=40, unit="KG", recurrence_type="DAILY")
        occ = g.occurrence(need, occurrence_date=datetime.utcnow() + timedelta(days=1), requested_quantity=40, unit="KG", status="MATCHED", quantity_matched=40)
        product = g.product_for(quantity_for_sale=50)
        g.allocation(occurrence=occ, producer=g.producer, product=product, quantity=40, unit_price=500, unit="KG")
    conn.close()
    return need, occ


# G + H — acceptation persistée ; rejeu => refus métier, aucun 2e event
def test_accept_emits_digest_accepted_once_and_replay_emits_nothing(market):
    dsn, _g, user, profile = market
    need, occ = _matched_occurrence(market)

    async def accept(session):
        return await _Svc(session, user, profile).accept_match_proposal(
            phone="+226", recurring_need_id=str(need), action="ACCEPT"
        )

    _run(dsn, accept)
    with pytest.raises(BusinessRuleException):
        _run(dsn, accept)

    async def fn(session):
        return await _events(session, "RECURRING_DIGEST_ACCEPTED", entity_id=occ)

    events = _run(dsn, fn)
    assert len(events) == 1
    assert events[0][1]["quantity"] == 40


# I + J — skip persisté ; rejeu => refus métier, aucun 2e event
def test_skip_emits_occurrence_skipped_once_and_replay_emits_nothing(market):
    dsn, _g, user, profile = market
    result = _create(market)
    need_id = result["recurring_need_id"]
    target = FIXED_TODAY + timedelta(days=5)  # les occurrences démarrent à aujourd'hui + 4 (délai minimal par défaut)

    async def skip(session):
        return await observed_update(
            _Svc(session, user, profile), session,
            phone="+226", recurring_need_id=need_id, action="OCCURRENCE_SKIP", occurrence_date=target
        )

    out = _run(dsn, skip)
    assert out["outcome"] == "APPLIED"
    assert _run(dsn, skip)["outcome"] == "ALREADY_APPLIED"  # B25 : rejeu idempotent, aucun 2e événement

    async def fn(session):
        return await _events(session, "RECURRING_OCCURRENCE_SKIPPED", entity_id=out["occurrence_id"])

    assert len(_run(dsn, fn)) == 1
