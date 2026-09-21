"""Cohérence Drizzle / SQLAlchemy / PostgreSQL sur une base RECONSTRUITE depuis zéro par les migrations officielles."""
from __future__ import annotations

import db_tools
import psycopg2
import pytest
import schema_model as m
from schema_policy import SITE_ONLY_TABLES


def _fail_if(divs, title):
    if divs:
        pytest.fail("\n" + m.render(divs, title), pytrace=False)


def test_migrations_from_empty_database_yield_the_drizzle_snapshot(drizzle_schema, pg_schema):
    """Base vide -> migrations -> catalogue PostgreSQL == snapshot Drizzle (tables, colonnes, FK, index, uniques)."""
    _fail_if(m.compare(drizzle_schema, pg_schema, "drizzle", "postgres"), "Drizzle != PostgreSQL")


def test_sqlalchemy_mirrors_the_real_postgres_schema(sqlalchemy_schema, pg_schema):
    """Le modèle Python décrit exactement ce que PostgreSQL contient (hors tables site-only)."""
    _fail_if(
        m.compare(sqlalchemy_schema, pg_schema, "sqlalchemy", "postgres", ignore_tables=SITE_ONLY_TABLES),
        "SQLAlchemy != PostgreSQL",
    )


def test_no_postgres_enum_type_exists(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute("select count(*) from pg_type where typtype = 'e'")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_required_extensions_are_created_by_migrations(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute("select extname from pg_extension")
        assert "pg_trgm" in {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


def test_every_foreign_key_is_validated_in_postgres(pg_dsn):
    """Une FK non validée (NOT VALID) n'est pas une garantie : aucune ne doit exister."""
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute("select conrelid::regclass::text, conname from pg_constraint where contype='f' and not convalidated")
        assert cur.fetchall() == []
    finally:
        conn.close()


def test_rebuild_from_scratch_is_reproducible(pg_schema):
    """Une 2e base reconstruite depuis zéro a EXACTEMENT le même schéma (déterminisme des migrations)."""
    admin = db_tools.admin_dsn()
    dsn, drop = db_tools.create_database(admin)
    try:
        db_tools.apply_migrations(dsn)
        conn = psycopg2.connect(dsn)
        try:
            again = {k: v for k, v in m.load_postgres(conn).items() if k[1] != "__drizzle_migrations"}
        finally:
            conn.close()
        _fail_if(m.compare(pg_schema, again, "build#1", "build#2"), "reconstruction non reproductible")
    finally:
        drop()


def test_orm_round_trip_uses_server_defaults_against_real_schema(pg_dsn):
    """Le ORM (modèles Python) insère/relit une chaîne de vente complète sur le VRAI schéma :
    prouve que noms de colonnes, types et défauts serveur sont réellement compatibles."""
    import uuid

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from ladini.domain.models import (
        BuyerProfile,
        Order,
        OrderItem,
        Payment,
        Producer,
        Product,
        User,
    )

    engine = create_engine(pg_dsn.replace("postgresql://", "postgresql+psycopg2://", 1))
    with Session(engine) as s, s.begin():
        s.begin_nested()  # tout est annulé à la fin
        seller = User(phone=f"+226{uuid.uuid4().hex[:8]}")
        buyer_u = User(phone=f"+226{uuid.uuid4().hex[:8]}")
        s.add_all([seller, buyer_u])
        s.flush()
        assert seller.role == "USER" or seller.role is None  # défaut applicatif ou serveur
        prod = Producer(user_id=seller.id)
        buyer = BuyerProfile(user_id=buyer_u.id)
        s.add_all([prod, buyer])
        s.flush()
        product = Product(name="Mil", category_label="Céréales", price=100, producer_id=prod.id)
        order = Order(total_amount=500, buyer_id=buyer.id)
        s.add_all([product, order])
        s.flush()
        s.add(OrderItem(order_id=order.id, product_id=product.id, quantity=5, price_at_sale=100))
        s.add(Payment(order_id=order.id, amount=500))
        s.flush()
        s.refresh(order)
        assert order.status == "PENDING" and order.payment_status == "PENDING" and order.currency == "XOF"
        s.rollback()
