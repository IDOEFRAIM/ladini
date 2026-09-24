"""P0 — une confirmation de besoin récurrent produit AU PLUS UN jeu de besoins, prouvé
contre un VRAI PostgreSQL (base reconstruite depuis les migrations Drizzle officielles).

Invariant : `(draft_id, execution_version)` -> au plus un ensemble de `recurring_needs`,
même en cas de retry HTTP/Celery, timeout MCP après COMMIT, « oui » répété, réconciliation
ou exécutions concurrentes. Garanti par la ligne `marketplace.recurring_need_drafts`
verrouillée (`FOR UPDATE`) et passée à EXECUTED dans la MÊME transaction que les besoins
(`services/database/recurring_supply.py::_open_confirmation/_close_confirmation`).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_recurring_supply_service import FIXED_TODAY

import ladini.services.database.recurring_supply as recurring_supply_module
from ladini.graphs.agents.market_coach.domain.recurring_need_draft import (
    RecurringNeedDraft,
    RecurringNeedDraftStatus,
)
from ladini.services.database import recurring_need_draft_store as store
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.errors import BusinessRuleException
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


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(recurring_supply_module, "_today", lambda: FIXED_TODAY)


@pytest.fixture
def market(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        cur.execute("update governance.sub_categories set name = %s where id = %s", ("tomate", g.sub_category))
        user, profile = SimpleNamespace(id=g.buyer_user), SimpleNamespace(id=g.buyer)
    conn.close()
    return SimpleNamespace(dsn=pg_dsn, user=user, profile=profile, buyer_id=g.buyer)


def _async_dsn(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


def _with_real_store(dsn, monkeypatch, coro_factory):
    """Exécute `coro_factory()` avec le VRAI store branché sur la base de test."""

    async def go():
        engine = create_async_engine(_async_dsn(dsn))
        monkeypatch.setattr(store, "get_sessionmaker", lambda: async_sessionmaker(engine, expire_on_commit=False))
        try:
            return await coro_factory()
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _executing_draft(draft_id: str) -> RecurringNeedDraft:
    draft = RecurringNeedDraft.new(
        draft_id=draft_id, product="tomate", quantity=40.0, unit="KG", recurrence_type="DAILY"
    )
    return draft._confirm_to_executing()


async def _call(engine, market, draft, **over):
    """UN appel métier = UNE transaction (commit si succès, rollback sinon), comme
    `@transactional(write=True)`."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        svc = _Svc(session, market.user, market.profile)
        kwargs = dict(
            phone="+226", product_query="tomate", quantity=40, unit="KG", recurrence_type="DAILY",
            draft_id=draft.draft_id, draft_version=draft.execution_version,
        )
        kwargs.update(over)
        try:
            result = await svc.create_recurring_need(**kwargs)
        except BaseException:
            await session.rollback()
            raise
        await session.commit()
        return result


def _needs_count(dsn, buyer_id) -> int:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("select count(*) from marketplace.recurring_needs where buyer_id = %s", (buyer_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


def _draft_row(dsn, draft_id):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select status, version, payload from marketplace.recurring_need_drafts where draft_id = %s",
                (draft_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


# ── store ────────────────────────────────────────────────────────────────


def test_store_round_trips_a_draft_and_enforces_cas(pg_dsn, monkeypatch):
    draft = RecurringNeedDraft.new(draft_id="rt-1", product="coq", quantity=14.0, unit="TETE", recurrence_type="WEEKLY",
                                   additional_items=[{"product": "chèvre", "quantity": 20.0, "unit": "TETE"}])

    async def scenario():
        assert await store.insert(draft, conversation_id="+226")
        assert not await store.insert(draft, conversation_id="+226"), "draft_id unique"
        loaded = await store.load("rt-1")
        executing = draft._confirm_to_executing()
        won = await store.compare_and_swap("rt-1", expected_version=draft.version, new_draft=executing)
        lost = await store.compare_and_swap("rt-1", expected_version=draft.version, new_draft=executing)
        return loaded, won, lost, await store.load("rt-1")

    loaded, won, lost, final = _with_real_store(pg_dsn, monkeypatch, scenario)
    assert loaded == draft
    assert won is True and lost is False
    assert final.status == RecurringNeedDraftStatus.EXECUTING and final.execution_version == final.version
    assert final.item_count() == 2


def test_in_doubt_drafts_are_found_for_reconciliation(pg_dsn, monkeypatch):
    async def scenario():
        await store.insert(_executing_draft("doubt-1"), conversation_id="+226")
        return await store.find_in_doubt(older_than_seconds=0), await store.find_in_doubt(older_than_seconds=3600)

    stale, fresh = _with_real_store(pg_dsn, monkeypatch, scenario)
    assert "doubt-1" in {d.draft_id for d, _ in stale}
    assert "doubt-1" not in {d.draft_id for d, _ in fresh}


# ── registre de confirmation ─────────────────────────────────────────────


def test_a_confirmation_executes_once_and_is_replayed_afterwards(market, monkeypatch):
    draft = _executing_draft("ledger-1")

    async def scenario():
        await store.insert(draft, conversation_id="+226")
        engine = create_async_engine(_async_dsn(market.dsn))
        try:
            first = await _call(engine, market, draft)
            replay = await _call(engine, market, draft)
            return first, replay, await store.load("ledger-1")
        finally:
            await engine.dispose()

    first, replay, final = _with_real_store(market.dsn, monkeypatch, scenario)
    assert first["status"] == "success" and not first.get("replayed")
    assert replay["replayed"] is True
    assert replay["recurring_need_id"] == first["recurring_need_id"]
    assert _needs_count(market.dsn, market.buyer_id) == 1
    assert final.status == RecurringNeedDraftStatus.EXECUTED
    assert final.execution_result["recurring_need_id"] == first["recurring_need_id"]


def test_a_failure_rolls_back_the_needs_and_leaves_the_confirmation_retryable(market, monkeypatch):
    draft = _executing_draft("ledger-2")

    async def scenario():
        await store.insert(draft, conversation_id="+226")
        engine = create_async_engine(_async_dsn(market.dsn))
        try:
            with pytest.raises(AttributeError):  # unit=None : échec après verrouillage du registre
                # l'item échoue APRÈS verrouillage du registre : tout doit être annulé
                await _call(engine, market, draft, unit=None)
            retried = await _call(engine, market, draft)
            return retried
        finally:
            await engine.dispose()

    retried = _with_real_store(market.dsn, monkeypatch, scenario)
    assert retried["status"] == "success" and not retried.get("replayed")
    assert _needs_count(market.dsn, market.buyer_id) == 1
    assert _draft_row(market.dsn, "ledger-2")[0] == "EXECUTED"


def test_a_stale_confirmation_version_is_refused(market, monkeypatch):
    draft = _executing_draft("ledger-3")

    async def scenario():
        await store.insert(draft, conversation_id="+226")
        engine = create_async_engine(_async_dsn(market.dsn))
        try:
            with pytest.raises(BusinessRuleException):
                await _call(engine, market, draft, draft_version=draft.execution_version - 1)
        finally:
            await engine.dispose()

    _with_real_store(market.dsn, monkeypatch, scenario)
    assert _needs_count(market.dsn, market.buyer_id) == 0
    assert _draft_row(market.dsn, "ledger-3")[0] == "EXECUTING"


def test_a_draft_that_was_never_confirmed_cannot_be_executed(market, monkeypatch):
    draft = RecurringNeedDraft.new(draft_id="ledger-4", product="tomate", quantity=40.0, unit="KG", recurrence_type="DAILY")

    async def scenario():
        await store.insert(draft, conversation_id="+226")
        engine = create_async_engine(_async_dsn(market.dsn))
        try:
            with pytest.raises(BusinessRuleException):
                await _call(engine, market, draft, draft_version=None)
            with pytest.raises(BusinessRuleException):
                await _call(engine, market, draft, draft_version=1)
        finally:
            await engine.dispose()

    _with_real_store(market.dsn, monkeypatch, scenario)
    assert _needs_count(market.dsn, market.buyer_id) == 0


def test_two_concurrent_executions_of_one_confirmation_create_one_set(market, monkeypatch):
    draft = _executing_draft("ledger-5")

    async def scenario():
        await store.insert(draft, conversation_id="+226")
        engines = [create_async_engine(_async_dsn(market.dsn)) for _ in range(2)]
        try:
            return await asyncio.gather(*[_call(e, market, draft) for e in engines])
        finally:
            for e in engines:
                await e.dispose()

    results = _with_real_store(market.dsn, monkeypatch, scenario)
    assert {r["recurring_need_id"] for r in results} == {results[0]["recurring_need_id"]}
    assert sorted(bool(r.get("replayed")) for r in results) == [False, True]
    assert _needs_count(market.dsn, market.buyer_id) == 1


def test_a_multi_item_confirmation_is_all_or_nothing_and_replayable(market, monkeypatch):
    draft = _executing_draft("ledger-6")
    items = [
        {"product_query": "tomate", "quantity": 10, "unit": "KG"},
        {"product_query": "tomate", "quantity": 20, "unit": "KG"},
    ]

    async def scenario():
        await store.insert(draft, conversation_id="+226")
        engine = create_async_engine(_async_dsn(market.dsn))
        try:
            async def batch(its):
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    svc = _Svc(session, market.user, market.profile)
                    try:
                        out = await svc.create_recurring_needs(
                            phone="+226", items=its, recurrence_type="DAILY",
                            draft_id=draft.draft_id, draft_version=draft.execution_version,
                        )
                    except BaseException:
                        await session.rollback()
                        raise
                    await session.commit()
                    return out

            with pytest.raises(AttributeError):  # unit=None : échec après verrouillage du registre
                await batch(items + [{"product_query": "tomate", "quantity": 5, "unit": None}])
            partial_after_failure = _needs_count(market.dsn, market.buyer_id)
            first = await batch(items)
            replay = await batch(items)
            return partial_after_failure, first, replay
        finally:
            await engine.dispose()

    partial, first, replay = _with_real_store(market.dsn, monkeypatch, scenario)
    assert partial == 0, "aucune création partielle"
    assert len(first["items"]) == 2 and replay["replayed"] is True
    assert [i["recurring_need_id"] for i in replay["items"]] == [i["recurring_need_id"] for i in first["items"]]
    assert _needs_count(market.dsn, market.buyer_id) == 2
    status, _version, payload = _draft_row(market.dsn, "ledger-6")
    assert status == "EXECUTED"
    stored = payload if isinstance(payload, dict) else json.loads(payload)
    assert len(stored["execution_result"]["items"]) == 2


def test_the_ledger_sql_matches_the_migrated_table(pg_dsn):
    """Le registre n'ajoute aucune colonne : il utilise la table migrée telle quelle."""
    conn = psycopg2.connect(pg_dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'marketplace' and table_name = 'recurring_need_drafts' order by 1"
            )
            columns = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    assert columns == {"draft_id", "conversation_id", "version", "status", "payload", "created_at", "updated_at"}
