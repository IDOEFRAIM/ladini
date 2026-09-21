"""Rétention de la télémétrie : purge batchée, cascade, garde de configuration, non-blocage (PostgreSQL réel)."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ladini.workers.crons import agent_telemetry_retention as ret

NOW = datetime.now(timezone.utc)


def _seed(dsn, tag, n_old, n_new):
    c = psycopg2.connect(dsn)
    with c, c.cursor() as cur:
        for i in range(n_old + n_new):
            old = i < n_old
            ts = NOW - timedelta(days=45 if old else 1)
            cur.execute(
                "insert into intelligence.agent_turns (conversation_id, phone_hash, outcome, response_status, started_at, completed_at,"
                " duration_ms, created_at, message_sid) values (%s,%s,'COMPLETED','SENT',%s,%s,10,%s,%s) returning id",
                (str(uuid.uuid4()), f"h-{tag}", ts, ts, ts, f"{tag}-{i}"),
            )
            tid = cur.fetchone()[0]
            cur.execute("insert into intelligence.agent_tool_calls (turn_id, seq, tool_name, started_at, completed_at, duration_ms, status)"
                        " values (%s,1,'t',%s,%s,1,'SUCCESS')", (tid, ts, ts))
            cur.execute("insert into intelligence.agent_llm_calls (turn_id, seq, kind, duration_ms, status) values (%s,1,'OTHER',1,'SUCCESS')", (tid,))
    c.close()


def _count(dsn, tag):
    c = psycopg2.connect(dsn)
    try:
        cur = c.cursor()
        cur.execute("select count(*), (select count(*) from intelligence.agent_tool_calls tc join intelligence.agent_turns t on t.id = tc.turn_id where t.phone_hash=%s),"
                    " (select count(*) from intelligence.agent_llm_calls lc join intelligence.agent_turns t on t.id = lc.turn_id where t.phone_hash=%s)"
                    " from intelligence.agent_turns where phone_hash=%s", (f"h-{tag}", f"h-{tag}", f"h-{tag}"))
        return cur.fetchone()
    finally:
        c.close()


def _purge(dsn, **kw):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        factory = async_sessionmaker(engine, expire_on_commit=False)

        @asynccontextmanager
        async def session():
            async with factory() as s:
                yield s

        try:
            return await ret.purge_expired(session_factory=session, pause_seconds=0, **kw)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_old_turns_and_their_children_are_purged_recent_ones_kept(pg_dsn):
    _seed(pg_dsn, "ret1", n_old=7, n_new=3)
    assert _count(pg_dsn, "ret1") == (10, 10, 10)
    res = _purge(pg_dsn, retention_days=30, batch_size=3)
    assert res["deleted"] >= 7 and res["batches"] >= 3  # lots successifs
    assert _count(pg_dsn, "ret1") == (3, 3, 3)          # les récents restent, enfants inclus ; aucun orphelin


def test_a_second_run_is_a_noop(pg_dsn):
    _seed(pg_dsn, "ret2", n_old=2, n_new=1)
    _purge(pg_dsn, retention_days=30)
    assert _purge(pg_dsn, retention_days=30)["deleted"] == 0
    assert _count(pg_dsn, "ret2")[0] == 1


def test_an_invalid_retention_can_never_wipe_the_table(pg_dsn):
    _seed(pg_dsn, "ret3", n_old=0, n_new=4)
    for bad in (0, -5):
        res = _purge(pg_dsn, retention_days=bad)
        assert res["status"] == "skipped" and res["deleted"] == 0
    assert _count(pg_dsn, "ret3")[0] == 4


def test_the_run_is_bounded_and_reports_pending_work(pg_dsn):
    _seed(pg_dsn, "ret4", n_old=6, n_new=0)
    res = _purge(pg_dsn, retention_days=30, batch_size=2, max_batches=2)
    assert res["deleted"] == 4 and res["status"] == "more_pending"
    assert _count(pg_dsn, "ret4")[0] == 2
    assert _purge(pg_dsn, retention_days=30, batch_size=2)["deleted"] == 2  # le reliquat est repris


def test_a_lock_held_by_another_transaction_aborts_the_batch_instead_of_blocking(pg_dsn):
    """Sous contention (ligne verrouillée par une autre transaction), la purge s'interrompt (lock_timeout) sans bloquer
    la production ni lever ; elle reprend au passage suivant."""
    _seed(pg_dsn, "ret5", n_old=1, n_new=0)
    holder = psycopg2.connect(pg_dsn)
    try:
        cur = holder.cursor()
        cur.execute("select id from intelligence.agent_turns where phone_hash='h-ret5' for update")
        res = _purge(pg_dsn, retention_days=30)
        assert res["status"] == "partial" and res["deleted"] == 0
    finally:
        holder.rollback()
        holder.close()
    assert _purge(pg_dsn, retention_days=30)["deleted"] == 1


def test_the_task_is_registered_and_scheduled():
    from ladini.api.celery_app import celery_app
    from ladini.workers.beat_schedule import BEAT_SCHEDULE

    assert "workers.agent_telemetry_retention" in celery_app.tasks
    assert BEAT_SCHEDULE["agent-telemetry-retention"]["task"] == "workers.agent_telemetry_retention"
