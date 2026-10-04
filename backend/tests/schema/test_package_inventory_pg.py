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


# ─────────────────────────────── B17 : writers, recurring, concurrence annulation/achat, rollback ───────────────────────


def test_price_only_writer_merge_persists_and_keeps_counts_in_real_jsonb(pg_dsn):
    from ladini.domain.package_inventory import (
        assert_package_inventory_consistency,
        merge_tiers_preserving_inventory,
    )

    pid = _seed(pg_dsn)

    async def go():
        engine = _engine(pg_dsn)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                product = await _locked(s, pid)
                incoming = [
                    {"tier_id": "ignored", "quantity": 0.5, "unit": "LITRE", "price": 650, "packaging": "bidon",
                     "base_unit_quantity": 0.5, "min_order_quantity": 1, "available_count": 999},  # compte périmé
                    {"tier_id": "x", "quantity": 0.33, "unit": "LITRE", "price": 350, "packaging": "bidon",
                     "base_unit_quantity": 0.33, "min_order_quantity": 1},
                ]
                product.pricing_tiers = merge_tiers_preserving_inventory(product.pricing_tiers, incoming)
                await s.commit()
        finally:
            await engine.dispose()

    asyncio.run(go())
    qty, counts = _state(pg_dsn, pid)
    assert counts == {"t500": 50, "t330": 100} and qty == 58.0

    async def reload_check():
        engine = _engine(pg_dsn)
        try:
            async with AsyncSession(engine) as s:
                product = (await s.execute(select(Product).where(Product.id == pid))).scalar_one()
                assert_package_inventory_consistency(product)
                assert {t["tier_id"]: t["price"] for t in product.pricing_tiers} == {"t500": 650, "t330": 350}
        finally:
            await engine.dispose()

    asyncio.run(reload_check())


def test_recurring_candidates_sql_never_selects_a_package_product(pg_dsn):
    """Le VRAI SQL du matching récurrent contre PostgreSQL : le produit conditionné est exclu, le simple reste."""
    from ladini.workers.automation.need_matching_service import _CANDIDATES_SQL

    conn = psycopg2.connect(pg_dsn)
    try:
        with conn, conn.cursor() as cur:
            g = Graph(cur)
            cur.execute(
                "update marketplace.products set unit='LITRE', quantity_for_sale=58, is_available=true, "
                "pricing_tiers=%s where id=%s", (Json(_TIERS), str(g.product)),
            )
            plain = g.product_for(unit="LITRE", quantity_for_sale=40)
            cur.execute("select sub_category_id from marketplace.products where id=%s", (str(g.product),))
            sub = cur.fetchone()[0]
            cur.execute(
                str(_CANDIDATES_SQL).replace(":sub_category_id", "%(sub)s"), {"sub": str(sub)}
            )
            rows = {str(r[0]) for r in cur.fetchall()}
        assert str(plain) in rows and str(g.product) not in rows
    finally:
        conn.close()


def test_concurrent_cancel_and_buy_end_consistent(pg_dsn):
    pid = _seed(pg_dsn)

    async def prepare():
        await _debit(pg_dsn, pid, "t500", 45, 0.5)  # 5 restants

    asyncio.run(prepare())

    async def restore_five():
        engine = _engine(pg_dsn)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                product = await _locked(s, pid)
                await asyncio.sleep(0.2)
                restore_stock_for_item(product, _item("t500", 45, 0.5))
                await s.commit()
        finally:
            await engine.dispose()

    async def go():
        return await asyncio.gather(restore_five(), _debit(pg_dsn, pid, "t500", 8, 0.5))

    _none, refusal = asyncio.run(go())
    qty, counts = _state(pg_dsn, pid)
    assert counts["t500"] >= 0 and qty >= 0
    # restitution d'abord : 50 - 8 = 42 ; achat d'abord : refusé (5 < 8) puis restitution : 50
    assert (refusal is None and counts["t500"] == 42) or (refusal is not None and counts["t500"] == 50)
    assert abs(qty - (counts["t500"] * 0.5 + counts["t330"] * 0.33)) < 0.002


def test_exception_after_the_variant_mutation_rolls_everything_back(pg_dsn):
    pid = _seed(pg_dsn)

    async def go():
        engine = _engine(pg_dsn)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as s:
                product = await _locked(s, pid)
                assert debit_stock_for_item(product, _item("t500", 10, 0.5)) is None
                raise RuntimeError("panne après la mutation, avant le commit")
        except RuntimeError:
            pass
        finally:
            await engine.dispose()

    asyncio.run(go())
    assert _state(pg_dsn, pid) == (58.0, {"t500": 50, "t330": 100})
