"""B22 — création unique d'un besoin récurrent : une confirmation = un besoin, un jeu d'événements, aucune notification.

Réutilise les helpers du registre de confirmation (`test_recurring_need_confirmation_ledger`) : même base, mêmes fixtures.
Aucune contrainte « acheteur + produit + quantité + fréquence UNIQUE » : deux engagements identiques démarrés à des dates
différentes sont LÉGITIMES — l'identité d'idempotence est le draft (`draft_id` + `execution_version`).
"""
from __future__ import annotations

import asyncio

import psycopg2
import test_recurring_need_confirmation_ledger as ledger
from sqlalchemy.ext.asyncio import create_async_engine
from test_recurring_need_confirmation_ledger import (
    _async_dsn,
    _call,
    _executing_draft,
    _needs_count,
    _with_real_store,
    store,
)

market = ledger.market  # fixtures du registre de confirmation, réexposées sans redéfinition
_fixed_today = ledger._fixed_today


def _count(dsn: str, query: str, params=()) -> int:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()[0]
    finally:
        conn.close()


def _events(dsn: str, event_name: str, buyer_id) -> int:
    return _count(dsn, "select count(*) from analytics.event_outbox where event_name = %s and payload::text like %s",
                  (event_name, f"%{buyer_id}%"))


def _notifications(dsn: str, buyer_id) -> int:
    return _count(dsn, "select count(*) from intelligence.notification_outbox where payload::text like %s", (f"%{buyer_id}%",))


def test_replayed_confirmation_creates_one_need_one_creation_event_and_no_notification(market, monkeypatch):
    draft = _executing_draft("b22-replay")

    async def scenario():
        await store.insert(draft, conversation_id="+226")
        engine = create_async_engine(_async_dsn(market.dsn))
        try:
            return [await _call(engine, market, draft) for _ in range(3)]
        finally:
            await engine.dispose()

    results = _with_real_store(market.dsn, monkeypatch, scenario)
    assert [bool(r.get("replayed")) for r in results] == [False, True, True]
    assert _needs_count(market.dsn, market.buyer_id) == 1
    assert _events(market.dsn, "RECURRING_NEED_CREATED", market.buyer_id) == 1, "un seul événement métier de création"
    created = results[0]["occurrences_created"]
    assert _events(market.dsn, "RECURRING_OCCURRENCE_CREATED", market.buyer_id) == created
    assert _notifications(market.dsn, market.buyer_id) == 0, "la création n'envoie aucune notification"


def test_four_concurrent_confirmations_of_one_draft_create_exactly_one_need_and_one_event(market, monkeypatch):
    draft = _executing_draft("b22-race")

    async def scenario():
        await store.insert(draft, conversation_id="+226")
        engines = [create_async_engine(_async_dsn(market.dsn)) for _ in range(4)]
        try:
            return await asyncio.gather(*[_call(e, market, draft) for e in engines])
        finally:
            for e in engines:
                await e.dispose()

    results = _with_real_store(market.dsn, monkeypatch, scenario)
    assert len({r["recurring_need_id"] for r in results}) == 1
    assert sorted(bool(r.get("replayed")) for r in results) == [False, True, True, True]
    assert _needs_count(market.dsn, market.buyer_id) == 1
    assert _events(market.dsn, "RECURRING_NEED_CREATED", market.buyer_id) == 1


def test_two_distinct_drafts_for_the_same_product_are_two_legitimate_needs(market, monkeypatch):
    first, second = _executing_draft("b22-twin-a"), _executing_draft("b22-twin-b")

    async def scenario():
        await store.insert(first, conversation_id="+226")
        await store.insert(second, conversation_id="+226")
        engine = create_async_engine(_async_dsn(market.dsn))
        try:
            a = await _call(engine, market, first, starts_at="2027-03-12")
            b = await _call(engine, market, second, starts_at="2027-03-17")
            return a, b
        finally:
            await engine.dispose()

    a, b = _with_real_store(market.dsn, monkeypatch, scenario)
    assert a["recurring_need_id"] != b["recurring_need_id"]
    assert _needs_count(market.dsn, market.buyer_id) == 2, "aucune unicité métier sur (acheteur, produit, quantité, fréquence)"
