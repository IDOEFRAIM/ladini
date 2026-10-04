"""B16 — inventaire par conditionnement contre un VRAI PostgreSQL (CI uniquement : `pg_dsn` saute sinon).

NON VALIDÉ EN LOCAL (pas de PostgreSQL sur la machine de développement) : exécuté par la CI.

Prouve : aller-retour JSONB des comptes, deux acheteurs concurrents sur la MÊME variante (un seul gagne, aucun
survente), variantes DIFFÉRENTES (les deux passent), refus sans mutation, restitution idempotente côté code
appelant (une ligne = une restitution), rollback sans effet.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import psycopg2
from factories import Graph
from psycopg2.extras import Json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.domain.catalog.models import Product
from ladini.domain.package_inventory import debit_stock_for_item, restore_stock_for_item

_TIERS = [
    {"tier_id": "t500", "quantity": 0.5, "unit": "LITRE", "price": 500, "packaging": "bidon",
     "base_unit_quantity": 0.5, "min_order_quantity": 1, "available_count": 50},
    {"tier_id": "t330", "quantity": 0.33, "unit": "LITRE", "price": 400, "packaging": "bidon",
     "base_unit_quantity": 0.33, "min_order_quantity": 1, "available_count": 100},
]


def _engine(dsn):
    return create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))


def _seed(dsn) -> str:
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            g = Graph(cur)
            cur.execute(
                "update marketplace.products set unit='LITRE', quantity_for_sale=58, pricing_tiers=%s where id=%s",
                (Json(_TIERS), str(g.product)),
            )
            return str(g.product)
    finally:
        conn.close()


def _item(tid, n, size):
    return SimpleNamespace(tier_id=tid, quantity=n, base_unit_quantity=n * size)


async def _locked(session, pid):
    return (await session.execute(select(Product).where(Product.id == pid).with_for_update())).scalar_one()


async def _debit(dsn, pid, tid, n, size, hold=0.0):
    engine = _engine(dsn)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            product = await _locked(s, pid)
            if hold:
                await asyncio.sleep(hold)
            refusal = debit_stock_for_item(product, _item(tid, n, size))
            await s.commit()
            return refusal
    finally:
        await engine.dispose()


def _state(dsn, pid):
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("select quantity_for_sale, pricing_tiers from marketplace.products where id=%s", (pid,))
            qty, tiers = cur.fetchone()
            return float(qty), {t["tier_id"]: t["available_count"] for t in tiers}
    finally:
        conn.close()


def test_counts_round_trip_in_jsonb(pg_dsn):
    pid = _seed(pg_dsn)
    assert _state(pg_dsn, pid) == (58.0, {"t500": 50, "t330": 100})


def test_two_buyers_same_variant_only_one_wins_no_oversell(pg_dsn):
    pid = _seed(pg_dsn)

    async def go():
        return await asyncio.gather(
            _debit(pg_dsn, pid, "t500", 30, 0.5, hold=0.3), _debit(pg_dsn, pid, "t500", 30, 0.5)
        )

    results = asyncio.run(go())
    assert sorted(r is None for r in results) == [False, True]  # exactement un refus
    qty, counts = _state(pg_dsn, pid)
    assert counts["t500"] == 20 and qty == 43.0


def test_two_buyers_different_variants_both_succeed(pg_dsn):
    pid = _seed(pg_dsn)

    async def go():
        return await asyncio.gather(_debit(pg_dsn, pid, "t500", 10, 0.5, hold=0.2), _debit(pg_dsn, pid, "t330", 20, 0.33))

    assert asyncio.run(go()) == [None, None]
    qty, counts = _state(pg_dsn, pid)
    assert counts == {"t500": 40, "t330": 80} and round(qty, 3) == 46.4


def test_oversell_refused_without_mutation_even_if_global_litres_suffice(pg_dsn):
    pid = _seed(pg_dsn)
    refusal = asyncio.run(_debit(pg_dsn, pid, "t500", 60, 0.5))
    assert refusal and refusal["reason"] == "insufficient_package_stock" and refusal["available_packages"] == 50
    assert _state(pg_dsn, pid) == (58.0, {"t500": 50, "t330": 100})


def test_restore_returns_package_and_litres_once_and_rollback_is_clean(pg_dsn):
    pid = _seed(pg_dsn)
    assert asyncio.run(_debit(pg_dsn, pid, "t500", 10, 0.5)) is None

    async def restore(commit):
        engine = _engine(pg_dsn)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                product = await _locked(s, pid)
                restore_stock_for_item(product, _item("t500", 10, 0.5))
                await (s.commit() if commit else s.rollback())
        finally:
            await engine.dispose()

    asyncio.run(restore(False))  # rollback : aucun effet
    assert _state(pg_dsn, pid)[1]["t500"] == 40
    asyncio.run(restore(True))
    assert _state(pg_dsn, pid) == (58.0, {"t500": 50, "t330": 100})
