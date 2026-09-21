"""Écriture réelle d'un tour dans PostgreSQL : colonnes, enfants, sessions, confidentialité, retries."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ladini.core import turn_telemetry as tt

PHONE = "+226 70 12 45 82"


def _persist(pg_dsn, rec, monkeypatch):
    async def go():
        engine = create_async_engine(pg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            monkeypatch.setattr("ladini.core.database.get_sessionmaker", lambda: async_sessionmaker(engine, expire_on_commit=False))
            await tt._persist(rec, 1234, datetime.now(timezone.utc))
        finally:
            await engine.dispose()

    asyncio.run(go())


def _rows(pg_dsn, sql, *args):
    c = psycopg2.connect(pg_dsn)
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        c.close()


def _rec(sid=None, retries=0, when=None, **kw):
    rec = tt.TurnRecorder(phone=PHONE, message_sid=sid, task_retries=retries,
                          user_message="Je veux vendre 50 kg de tomates, appelle-moi au +226 70 12 45 82", **kw)
    if when:
        rec.started_at = when
    rec.response_text = "Votre offre a été créée."
    rec.response_status = "SENT"
    return rec


def test_a_full_turn_is_persisted_with_children_and_no_personal_data(pg_dsn, monkeypatch):
    rec = _rec(sid="SM_PERSIST_1")
    tt.begin_turn(rec)
    tt.note_final({"detected_intent": "SALES_PUBLISH", "interpreter_confidence": 0.96, "current_goal": "SALES_PUBLISH",
                   "pending_interaction": {"kind": "ENTER_PRICE"}, "goal_status": "WAITING_INPUT", "user_role": "PRODUCER"})
    rec.add_sql(12.5)
    rec.add_sql(30.0)
    rec.add_redis(2.0)
    tt.note_tool("find_product", "READ", datetime.now(timezone.utc), 0.087, "SUCCESS")
    tt.note_tool("create_listing", "WRITE", datetime.now(timezone.utc), 0.143, "ERROR", ValueError("x"))
    tt.note_llm(name="llm_gateway_completion", model="llama", provider="groq", profile="INTERPRETER", agent_node=None,
                latency_s=0.51, usage={"prompt_tokens": 100, "completion_tokens": 20}, error=None, fallback_from=None)
    tt.end_turn()
    _persist(pg_dsn, rec, monkeypatch)

    ((intent, wf, step, outcome, dbn, dbms, dbmax, rn, mcp, mcpms, llm, ill, phash, last4, umsg, resp, dur),) = _rows(
        pg_dsn,
        "select intent, workflow, workflow_step, outcome, db_query_count, db_duration_ms, db_max_query_ms, redis_command_count,"
        " mcp_call_count, mcp_duration_ms, llm_call_count, intent_llm_duration_ms, phone_hash, phone_last4,"
        " user_message_excerpt, agent_response_excerpt, duration_ms from intelligence.agent_turns where message_sid='SM_PERSIST_1'",
    )
    assert (intent, wf, step, outcome) == ("SALES_PUBLISH", "SALES_PUBLISH", "ENTER_PRICE", "WAITING_USER")
    assert (dbn, dbms, dbmax, rn, mcp, mcpms, llm, ill, dur) == (2, 42, 30, 1, 2, 230, 1, 510, 1234)
    assert last4 == "4582" and len(phash) == 64
    # confidentialité : aucun numéro en clair nulle part
    assert "70 12 45" not in (umsg or "") and "[tel]" in umsg and "tomates" in umsg and resp == "Votre offre a été créée."
    dump = " ".join(str(v) for r in _rows(pg_dsn, "select * from intelligence.agent_turns where message_sid='SM_PERSIST_1'") for v in r)
    assert "70124582" not in dump and "+226 70" not in dump
    tools = _rows(pg_dsn, "select seq, tool_name, tool_category, status, error_category from intelligence.agent_tool_calls "
                          "where turn_id=(select id from intelligence.agent_turns where message_sid='SM_PERSIST_1') order by seq")
    assert tools == [(1, "find_product", "READ", "SUCCESS", None), (2, "create_listing", "WRITE", "ERROR", "TOOL")]
    ((prov, kind, tok),) = _rows(pg_dsn, "select provider, kind, prompt_tokens from intelligence.agent_llm_calls "
                                         "where turn_id=(select id from intelligence.agent_turns where message_sid='SM_PERSIST_1')")
    assert (prov, kind, tok) == ("groq", "INTERPRETER", 100)


def test_turns_of_the_same_user_share_a_session_until_the_gap_is_exceeded(pg_dsn, monkeypatch):
    now = datetime.now(timezone.utc)
    _persist(pg_dsn, _rec(sid="SM_S1", when=now), monkeypatch)
    _persist(pg_dsn, _rec(sid="SM_S2", when=now + timedelta(minutes=5)), monkeypatch)
    _persist(pg_dsn, _rec(sid="SM_S3", when=now + timedelta(minutes=5 + 31)), monkeypatch)
    c = dict(_rows(pg_dsn, "select message_sid, conversation_id::text from intelligence.agent_turns where message_sid in ('SM_S1','SM_S2','SM_S3')"))
    assert c["SM_S1"] == c["SM_S2"] and c["SM_S3"] != c["SM_S2"]


def test_a_celery_retry_is_a_new_row_and_a_replayed_write_is_rejected(pg_dsn, monkeypatch):
    _persist(pg_dsn, _rec(sid="SM_R1", retries=0), monkeypatch)
    _persist(pg_dsn, _rec(sid="SM_R1", retries=1), monkeypatch)
    assert _rows(pg_dsn, "select count(*) from intelligence.agent_turns where message_sid='SM_R1'") == [(2,)]
    # une même tentative réécrite : violation d'unicité -> avalée par finish_and_persist (jamais vers l'appelant)
    async def go():
        engine = create_async_engine(pg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            monkeypatch.setattr("ladini.core.database.get_sessionmaker", lambda: async_sessionmaker(engine, expire_on_commit=False))
            return await tt.finish_and_persist(_rec(sid="SM_R1", retries=1))
        finally:
            await engine.dispose()

    before = tt.write_failures()
    assert asyncio.run(go()) is False and tt.write_failures() == before + 1


def test_otp_context_keeps_no_message_content(pg_dsn, monkeypatch):
    rec = _rec(sid="SM_OTP")
    rec.user_message = "4821"
    rec.workflow = "VERIFY_DELIVERY_OTP"
    _persist(pg_dsn, rec, monkeypatch)
    assert _rows(pg_dsn, "select user_message_excerpt from intelligence.agent_turns where message_sid='SM_OTP'") == [("[contenu masqué]",)]


@pytest.mark.parametrize("bad", ["not-a-uuid", ""])
def test_a_malformed_user_id_never_breaks_the_write(pg_dsn, monkeypatch, bad):
    rec = _rec(sid=f"SM_BADUID_{len(bad)}")
    rec.user_id = bad
    _persist(pg_dsn, rec, monkeypatch)
    assert _rows(pg_dsn, "select user_id from intelligence.agent_turns where message_sid=%s", rec.message_sid) == [(None,)]
