"""Tables de télémétrie de l'agent : existence, FK, contraintes CHECK, unicité, cascade (PostgreSQL réel)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from factories import insert, uniq
from psycopg2 import errors

NOW = datetime.now(timezone.utc)


def _turn(cur, **over):
    cols = dict(
        conversation_id=str(uuid.uuid4()), phone_hash=uniq("h"), outcome="COMPLETED", response_status="SENT",
        started_at=NOW, completed_at=NOW, duration_ms=120,
    )
    cols.update(over)
    return insert(cur, "intelligence.agent_turns", **cols)


def _expect(db, exc, fn):
    cur = db.cursor()
    cur.execute("SAVEPOINT chk")
    with pytest.raises(exc):
        fn()
    cur.execute("ROLLBACK TO SAVEPOINT chk")


def test_a_full_turn_with_tool_and_llm_calls_is_accepted(db):
    cur = db.cursor()
    turn = _turn(cur, intent="CREATE_LISTING", intent_confidence=0.96, workflow="SALES_PUBLISH", workflow_step="ENTER_PRICE")
    insert(cur, "intelligence.agent_tool_calls", turn_id=turn, seq=1, tool_name="find_product", started_at=NOW,
           completed_at=NOW, duration_ms=87, status="SUCCESS", tool_category="READ")
    insert(cur, "intelligence.agent_llm_calls", turn_id=turn, seq=1, kind="INTERPRETER", provider="groq",
           model="llama", duration_ms=510, status="SUCCESS")
    cur.execute("select db_query_count, mcp_call_count, channel from intelligence.agent_turns where id=%s", (turn,))
    assert cur.fetchone() == (0, 0, "WHATSAPP")  # défauts serveur


@pytest.mark.parametrize("col,val", [
    ("outcome", "SUCCESS"), ("response_status", "OK"), ("channel", "SMS"), ("error_category", "WHATEVER"), ("duration_ms", -1),
])
def test_invalid_turn_values_are_rejected(db, col, val):
    cur = db.cursor()
    _expect(db, errors.CheckViolation, lambda: _turn(cur, **{col: val}))


def test_invalid_tool_and_llm_values_are_rejected(db):
    cur = db.cursor()
    t = _turn(cur)
    base = dict(turn_id=t, seq=1, started_at=NOW, completed_at=NOW, duration_ms=1)
    _expect(db, errors.CheckViolation, lambda: insert(cur, "intelligence.agent_tool_calls", tool_name="x", status="OK", **base))
    _expect(db, errors.CheckViolation, lambda: insert(cur, "intelligence.agent_tool_calls", tool_name="x", status="SUCCESS", tool_category="Z", **base))
    _expect(db, errors.CheckViolation, lambda: insert(cur, "intelligence.agent_llm_calls", turn_id=t, seq=1, kind="OTHER", duration_ms=1, status="MAYBE"))
    _expect(db, errors.CheckViolation, lambda: insert(cur, "intelligence.agent_llm_calls", turn_id=t, seq=1, kind="X", duration_ms=1, status="SUCCESS"))


def test_orphan_children_are_rejected_and_children_follow_their_turn(db):
    cur = db.cursor()
    ghost = str(uuid.uuid4())
    _expect(db, errors.ForeignKeyViolation, lambda: insert(cur, "intelligence.agent_tool_calls", turn_id=ghost, seq=1, tool_name="x",
            started_at=NOW, completed_at=NOW, duration_ms=1, status="SUCCESS"))
    t = _turn(cur)
    insert(cur, "intelligence.agent_tool_calls", turn_id=t, seq=1, tool_name="x", started_at=NOW, completed_at=NOW, duration_ms=1, status="SUCCESS")
    insert(cur, "intelligence.agent_llm_calls", turn_id=t, seq=1, kind="RESPONSE", duration_ms=1, status="SUCCESS")
    cur.execute("delete from intelligence.agent_turns where id=%s", (t,))  # purge de rétention : les enfants suivent
    cur.execute("select (select count(*) from intelligence.agent_tool_calls where turn_id=%s)+(select count(*) from intelligence.agent_llm_calls where turn_id=%s)", (t, t))
    assert cur.fetchone()[0] == 0


def test_a_celery_retry_does_not_duplicate_a_turn(db):
    cur = db.cursor()
    sid = uniq("SM")
    _turn(cur, message_sid=sid, task_retries=0)
    _expect(db, errors.UniqueViolation, lambda: _turn(cur, message_sid=sid, task_retries=0))
    _turn(cur, message_sid=sid, task_retries=1)  # une vraie nouvelle tentative est une ligne distincte
    _turn(cur)
    _turn(cur)  # sans message_sid : illimité


def test_deleting_a_user_keeps_their_turns_detached(db):
    cur = db.cursor()
    u = insert(cur, "auth.users", phone=uniq("+226"))
    t = _turn(cur, user_id=u)
    cur.execute("delete from auth.users where id=%s", (u,))
    cur.execute("select user_id from intelligence.agent_turns where id=%s", (t,))
    assert cur.fetchone() == (None,)
