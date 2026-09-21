from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import db_tools  # noqa: E402
import schema_model as m  # noqa: E402


@pytest.fixture(scope="session")
def drizzle_schema() -> m.Schema:
    return m.load_drizzle(db_tools.SNAPSHOT)


@pytest.fixture(scope="session")
def sqlalchemy_schema() -> m.Schema:
    import ladini.domain.models  # noqa: F401  (enregistre modèles + tables runtime)
    from ladini.domain.orm_base import Base

    return m.load_sqlalchemy(Base.metadata)


@pytest.fixture(scope="session")
def pg_dsn():
    """DSN d'une base fraîche reconstruite UNIQUEMENT depuis les migrations officielles."""
    admin = db_tools.admin_dsn()
    if not admin:
        if os.environ.get("REQUIRE_SCHEMA_DB") == "1":
            pytest.fail("SCHEMA_TEST_DSN absent alors que REQUIRE_SCHEMA_DB=1 (CI) : test de schéma impossible.")
        pytest.skip("SCHEMA_TEST_DSN non défini — tests PostgreSQL de schéma ignorés en local.")
    dsn, drop = db_tools.create_database(admin)
    try:
        db_tools.apply_migrations(dsn)
        yield dsn
    finally:
        drop()


@pytest.fixture(scope="session")
def pg_schema(pg_dsn) -> m.Schema:
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        return {k: v for k, v in m.load_postgres(conn).items() if k[1] != "__drizzle_migrations"}
    finally:
        conn.close()


@pytest.fixture
def db(pg_dsn):
    """Connexion PostgreSQL ; tout est annulé (ROLLBACK) à la fin du test."""
    import psycopg2

    conn = psycopg2.connect(pg_dsn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
