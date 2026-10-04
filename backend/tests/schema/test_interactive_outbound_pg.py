"""B20 — `get_last_interactive_outbound` contre un VRAI PostgreSQL + descripteur d'interaction du digest.

Contrat : seuls les messages SENT dont le template attend une réponse nue (`INTERACTIVE_TEMPLATE_OWNERS`) sont retournés ;
une notification informative plus récente ne les masque pas ; passé le TTL du digest, rien n'est retourné.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg2
from psycopg2.extras import Json
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.domain.models  # noqa: F401  (tous les mappers)
from ladini.domain.recurring_supply.digest import (
    NeedAvailability,
    build_digest_text,
    digest_menu_actions,
)
from ladini.services.database.moderation import ModerationMixin
from ladini.workers.outbox import templates

PHONE = "+22670001234"


class _Svc(ModerationMixin):
    def __init__(self, session):
        self._s = session

    @property
    def session(self):
        return self._s


def _insert(cur, template, *, status="SENT", sent_at=None, payload=None, phone=PHONE):
    cur.execute(
        "insert into intelligence.notification_outbox (channel, recipient_phone, template_key, payload, dedupe_key, status, sent_at) "
        "values ('WHATSAPP', %s, %s, %s, %s, %s, %s)",
        (phone, template, Json(payload or {}), f"t:{uuid.uuid4()}", status, sent_at),
    )


def _call(dsn):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine) as s:
                return await _Svc(s).get_last_interactive_outbound(PHONE)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_returns_the_interactive_digest_not_the_newer_informational_notification(pg_dsn):
    now = datetime.utcnow()
    conn = psycopg2.connect(pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("delete from intelligence.notification_outbox")
            _insert(cur, templates.RECURRING_SUPPLY_DIGEST_BUYER, sent_at=now - timedelta(minutes=30), payload={
                "body": "x", "interactive": {"menu_id": "sig", "actions": {"1": "VIEW_DETAILS", "2": "MY_NEEDS"}},
                "occurrences": [{"recurring_need_id": "N1", "occurrence_id": "O1", "version": 1}]})
            _insert(cur, templates.ESCROW_PAYMENT_RECEIVED_BUYER, sent_at=now - timedelta(minutes=1))  # informatif, plus récent
            _insert(cur, templates.RECURRING_SUPPLY_ORDER_DELIVERED_BUYER, status="PENDING", sent_at=None)  # pas encore envoyé
    finally:
        conn.close()
    res = _call(pg_dsn)["interactive"]
    assert res["owner_type"] == "RECURRING_DIGEST" and res["menu_id"] == "sig"
    assert res["actions"] == {"1": "VIEW_DETAILS", "2": "MY_NEEDS"}
    assert res["recurring_need_ids"] == ["N1"] and res["occurrence_ids"] == ["O1"]
    assert abs(res["sent_at"] - (now - timedelta(minutes=30)).replace(tzinfo=timezone.utc).timestamp()) < 2


def test_newest_interactive_wins_and_expired_or_foreign_ones_are_ignored(pg_dsn):
    now = datetime.utcnow()
    conn = psycopg2.connect(pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("delete from intelligence.notification_outbox")
            _insert(cur, templates.RECURRING_SUPPLY_DIGEST_BUYER, sent_at=now - timedelta(hours=2), payload={"interactive": {"actions": None}})
            _insert(cur, templates.RECURRING_SUPPLY_ORDER_DELIVERED_BUYER, sent_at=now - timedelta(minutes=5))
            _insert(cur, templates.RECURRING_SUPPLY_DIGEST_BUYER, sent_at=now - timedelta(minutes=1), phone="+22670009999")
    finally:
        conn.close()
    assert _call(pg_dsn)["interactive"]["owner_type"] == "ORDER_RECEPTION"
    conn = psycopg2.connect(pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("update intelligence.notification_outbox set sent_at = sent_at - interval '3 days'")  # > TTL (20 h)
    finally:
        conn.close()
    assert _call(pg_dsn)["interactive"] is None


def test_digest_menu_descriptor_matches_the_text_actually_shown():
    none = NeedAvailability(product="tomate", requested_quantity=Decimal(350), matched_quantity=Decimal(0), unit="KG")
    some = NeedAvailability(product="tomate", requested_quantity=Decimal(350), matched_quantity=Decimal(100), unit="KG")
    assert digest_menu_actions([none]) == {"1": "VIEW_DETAILS", "2": "MY_NEEDS"}
    assert "1. Voir les détails" in build_digest_text([none]) and "2. Mes besoins" in build_digest_text([none])
    assert digest_menu_actions([some]) is None  # digest « confirmer / modifier / pas demain » : pas de menu numéroté
    assert "1. Voir les détails" not in build_digest_text([some])
