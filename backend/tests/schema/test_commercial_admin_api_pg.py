"""Espace COMMERCIAL — comportement SQL réel (agrégation par utilisateur, jointures
agent_turns/commercial_followups/audit_logs) contre une base PostgreSQL fraîche,
reconstruite UNIQUEMENT depuis les migrations officielles (voir conftest.py::pg_dsn).

Source de la liste/du détail : `intelligence.agent_turns` (télémétrie réelle, alimentée à
chaque tour WhatsApp/webchat) — PAS `intelligence.conversations`, qui n'est pas alimentée
par l'agent en production (même source que le cockpit monitoring existant).

Isolated day base (2030-01-01) : loin de tous les autres compteurs `_fresh_day()`/`_day()`
de ce dossier (2005/2010+), sur une base partagée au niveau de la session de test.

`WhatsAppChannel.send` est monkeypatché : ces tests valident l'agrégation SQL et l'audit
trail, jamais un envoi réseau réel (voir tests/unit/test_commercial_admin_api.py pour la
validation pure et l'invariant anti-LangGraph)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from factories import insert, uniq
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.services.commercial.admin_api as api
from ladini.workers.outbox.channels.base import SendResult


def _run(dsn, fn):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _no_real_whatsapp(monkeypatch):
    async def _send(self, *, body, recipient_phone=None, **kw):
        return SendResult.success(provider_ref="SM-fake")

    monkeypatch.setattr(api.WhatsAppChannel, "send", _send)


@pytest.fixture
def user(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        uid = insert(cur, "auth.users", phone=uniq("+226"), name="Aminata", role="BUYER")
    conn.close()
    return uid


def _turn(pg_dsn, user_id, *, created_at, intent=None, user_message="Bonjour", agent_response="ok"):
    """Un tour d'agent réel (`intelligence.agent_turns`) — la table qu'exploite
    réellement `admin_api.list_conversations`/`get_conversation_detail`."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        insert(
            cur,
            "intelligence.agent_turns",
            conversation_id=uuid.uuid4(),
            user_id=user_id,
            phone_hash=uniq("hash"),
            phone_last4="1234",
            intent=intent,
            outcome="COMPLETED",
            started_at=created_at,
            completed_at=created_at,
            duration_ms=100,
            response_status="SENT",
            user_message_excerpt=user_message,
            agent_response_excerpt=agent_response,
            created_at=created_at,
        )
    conn.close()


class TestListConversations:
    def test_aggregates_turns_and_last_activity_per_user(self, pg_dsn, user):
        t0 = datetime(2030, 1, 1, tzinfo=timezone.utc)
        _turn(pg_dsn, user, created_at=t0, intent="SALES_PUBLISH_PRODUCT")
        _turn(pg_dsn, user, created_at=t0 + timedelta(hours=1), intent="SALES_PUBLISH_PRODUCT_PRICE")

        result = _run(pg_dsn, lambda s: api.list_conversations(s, api.ListParams()))
        row = next(r for r in result["items"] if r["user_id"] == str(user))
        assert row["turns"] == 2
        assert row["last_intent"] == "SALES_PUBLISH_PRODUCT_PRICE"

    def test_to_follow_up_filter_matches_the_commercial_status_table(self, pg_dsn, user):
        _turn(pg_dsn, user, created_at=datetime(2030, 1, 2, tzinfo=timezone.utc))
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            insert(cur, "intelligence.commercial_followups", user_id=user, status="TO_FOLLOW_UP")
        conn.close()

        result = _run(pg_dsn, lambda s: api.list_conversations(s, api.ListParams(status_filter="to_follow_up")))
        assert any(r["user_id"] == str(user) for r in result["items"])

        result_resolved = _run(pg_dsn, lambda s: api.list_conversations(s, api.ListParams(status_filter="resolved")))
        assert not any(r["user_id"] == str(user) for r in result_resolved["items"])

    def test_long_filter_uses_the_configured_threshold(self, pg_dsn, user, monkeypatch):
        monkeypatch.setattr(api.settings, "COMMERCIAL_LONG_CONVERSATION_TURNS", 2)
        base = datetime(2030, 1, 3, tzinfo=timezone.utc)
        for i in range(3):
            _turn(pg_dsn, user, created_at=base + timedelta(minutes=i))

        result = _run(pg_dsn, lambda s: api.list_conversations(s, api.ListParams(status_filter="long")))
        row = next(r for r in result["items"] if r["user_id"] == str(user))
        assert row["is_long"] is True and row["turns"] == 3


class TestEveryReachableUserIsListed:
    """Le commercial peut contacter TOUT utilisateur ayant un numéro WhatsApp, même sans aucun échange avec l'agent."""

    def test_a_user_without_any_turn_is_listed_with_zero_turns(self, pg_dsn):
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            silent = insert(cur, "auth.users", phone=uniq("+226"), name="Silencieux", role="PRODUCER")
        conn.close()
        result = _run(pg_dsn, lambda s: api.list_conversations(s, api.ListParams(limit=200)))
        row = next(r for r in result["items"] if r["user_id"] == str(silent))
        assert row["turns"] == 0 and row["last_activity"] is None and row["is_long"] is False
        assert row["commercial_status"] == "NONE"

    def test_a_user_without_a_phone_is_not_listed(self, pg_dsn):
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            cur.execute("select count(*) from auth.users where phone is null or phone = ''")
            no_phone_before = cur.fetchone()[0]
        conn.close()
        result = _run(pg_dsn, lambda s: api.list_conversations(s, api.ListParams(limit=200)))
        assert all(r["phone_masked"] for r in result["items"]) or no_phone_before == 0

    def test_search_and_role_filters(self, pg_dsn):
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            target = insert(cur, "auth.users", phone=uniq("+226"), name="ZorroUnique", role="BUYER")
            other = insert(cur, "auth.users", phone=uniq("+226"), name="Autre", role="PRODUCER")
        conn.close()
        by_name = _run(pg_dsn, lambda s: api.list_conversations(s, api.ListParams(q="zorrounique")))
        assert [r["user_id"] for r in by_name["items"]] == [str(target)]
        by_role = _run(pg_dsn, lambda s: api.list_conversations(s, api.ListParams(role="BUYER", limit=200)))
        assert str(target) in {r["user_id"] for r in by_role["items"]} and str(other) not in {r["user_id"] for r in by_role["items"]}

    def test_the_detail_of_a_silent_user_is_an_empty_timeline_not_an_error(self, pg_dsn):
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            silent = insert(cur, "auth.users", phone=uniq("+226"), name="Muet", role="BUYER")
        conn.close()
        detail = _run(pg_dsn, lambda s: api.get_conversation_detail(s, str(silent)))
        assert detail["timeline"] == [] and detail["name"] == "Muet"


class TestConversationDetailTimeline:
    def test_merges_turns_and_commercial_outbound_in_chronological_order(self, pg_dsn, user):
        # `audit_logs.created_at` est écrit par `func.now()` côté serveur (non
        # paramétrable depuis `send_follow_up`) — le tour utilisateur doit donc
        # être daté dans le PASSÉ RÉCENT réel (pas une date arbitraire future
        # comme les autres tests de ce fichier), sinon il apparaîtrait APRÈS
        # la relance commerciale dans le tri chronologique.
        t0 = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        _turn(pg_dsn, user, created_at=t0, agent_response="Bonjour, que puis-je faire ?")

        async def _send_followup(session):
            conn = psycopg2.connect(pg_dsn)
            with conn, conn.cursor() as cur:
                commercial_id = insert(cur, "auth.users", phone=uniq("+226"))
            conn.close()
            return await api.send_follow_up(
                session, actor_id=str(commercial_id), user_id_raw=str(user), message="Toujours dispo ?"
            )

        sent = _run(pg_dsn, _send_followup)
        assert sent["sent"] is True

        detail = _run(pg_dsn, lambda s: api.get_conversation_detail(s, str(user)))
        roles = [m["role"] for m in detail["timeline"]]
        assert roles[0] == "USER"
        assert "COMMERCIAL" in roles
        commercial_msg = next(m for m in detail["timeline"] if m["role"] == "COMMERCIAL")
        assert commercial_msg["text"] == "Toujours dispo ?"
        # La relance a fait passer le statut de NONE -> FOLLOWED_UP.
        assert detail["commercial_status"] == "FOLLOWED_UP"


class TestSendFollowUpGuards:
    def test_refuses_to_send_to_a_blocked_account(self, pg_dsn):
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            blocked = insert(cur, "auth.users", phone=uniq("+226"), account_status="BLOCKED")
            actor = insert(cur, "auth.users", phone=uniq("+226"))
        conn.close()

        with pytest.raises(api.ApiError) as e:
            _run(pg_dsn, lambda s: api.send_follow_up(s, actor_id=str(actor), user_id_raw=str(blocked), message="hello"))
        assert e.value.status == 409


class TestUpdateStatus:
    def test_transition_is_persisted_and_audited(self, pg_dsn, user):
        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            actor = insert(cur, "auth.users", phone=uniq("+226"))
        conn.close()

        result = _run(
            pg_dsn,
            lambda s: api.update_status(s, actor_id=str(actor), user_id_raw=str(user), status="to_follow_up"),
        )
        assert result == {"status": "TO_FOLLOW_UP"}

        conn = psycopg2.connect(pg_dsn)
        with conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM intelligence.commercial_followups WHERE user_id = %s", (str(user),))
            assert cur.fetchone()[0] == "TO_FOLLOW_UP"
            cur.execute(
                "SELECT action FROM intelligence.audit_logs WHERE entity_id = %s AND entity_type = 'conversation'",
                (str(user),),
            )
            assert cur.fetchone()[0] == "COMMERCIAL_STATUS_CHANGE"
        conn.close()
