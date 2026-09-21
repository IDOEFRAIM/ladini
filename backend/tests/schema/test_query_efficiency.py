"""Requêtes efficaces ET fonctionnellement identiques, sur un vrai PostgreSQL.

Compte les instructions SQL réellement émises : le nombre doit rester CONSTANT quand le panier grandit
(pas de N+1), et le résultat métier doit rester exact.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.services.database.buyer import BuyerMixin


class _Svc(BuyerMixin):
    """BuyerMixin branché sur une session réelle (la propriété `session` est normalement fournie par le service central)."""

    def __init__(self, session, user):
        self._s = session
        self._user = user

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._user, None


@pytest.fixture
def market(pg_dsn):
    """Un acheteur en zone A ; des produits chez des producteurs de la même zone, d'une zone soeur, d'une zone lointaine."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        region = insert(cur, "governance.climatic_regions", name=uniq("r"))

        def zone(parent=None):
            return insert(cur, "governance.zones", name=uniq("z"), code=uniq("Z"), climatic_region_id=region, parent_id=parent)

        parent = zone()
        zone_a, zone_sibling = zone(parent), zone(parent)
        zone_far = zone()
        cur.execute("update auth.users set zone_id=%s where id=%s", (zone_a, g.buyer_user))

        def product_in(z):
            u = insert(cur, "auth.users", phone=uniq("+226"))
            pr = insert(cur, "marketplace.producers", user_id=u, zone_id=z)
            return insert(cur, "marketplace.products", category_label="c", price=10, producer_id=pr)

        products = {
            "same": [product_in(zone_a) for _ in range(3)],
            "sibling": [product_in(zone_sibling) for _ in range(3)],
            "far": [product_in(zone_far) for _ in range(3)],
        }
        buyer_user = SimpleNamespace(zone_id=uuid.UUID(zone_a), name="Acheteur")
    conn.close()
    return pg_dsn, buyer_user, products


def _run_counted(dsn, fn):
    statements: list[str] = []

    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        event.listen(engine.sync_engine, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
        try:
            async with AsyncSession(engine) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(go()), statements


def test_estimate_delivery_cost_is_exact_and_uses_constant_queries(market):
    dsn, user, p = market

    def cost(ids):
        async def fn(session):
            return await _Svc(session, user).estimate_delivery_cost("+226", [str(i) for i in ids])

        return _run_counted(dsn, fn)

    # même zone 1500, zone soeur (même parent) 4000, zone lointaine 12000
    res, small_sql = cost([p["same"][0], p["sibling"][0], p["far"][0]])
    assert res["status"] == "success" and res["estimated_cost"] == 1500 + 4000 + 12000

    big_ids = p["same"] + p["sibling"] + p["far"]
    res, big_sql = cost(big_ids)
    assert res["estimated_cost"] == 3 * (1500 + 4000 + 12000)
    assert len(big_sql) == len(small_sql) <= 2, f"N+1 : {len(small_sql)} requêtes pour 3 produits, {len(big_sql)} pour 9"


def test_estimate_delivery_cost_ignores_unknown_products_and_counts_duplicates(market):
    dsn, user, p = market
    ghost = "00000000-0000-0000-0000-000000000001"

    async def fn(session):
        return await _Svc(session, user).estimate_delivery_cost("+226", [str(p["same"][0]), str(p["same"][0]), ghost])

    res, _ = _run_counted(dsn, fn)
    assert res["estimated_cost"] == 3000  # doublon compté deux fois, produit inconnu ignoré (comportement historique)
