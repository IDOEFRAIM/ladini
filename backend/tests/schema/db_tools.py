"""Outils de base de données pour les tests de cohérence de schéma.

Une base PostgreSQL VIDE est créée par test-session, les migrations Drizzle
(`schema_contract/migrations`) y sont rejouées, puis la base est supprimée.
Configuration : `SCHEMA_TEST_DSN` = URL d'un serveur où le rôle peut `CREATE DATABASE`
(ex. `postgresql://postgres:postgres@localhost:5432/postgres`). En CI `REQUIRE_SCHEMA_DB=1`
transforme l'absence de DSN en ÉCHEC (jamais en skip silencieux).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

CONTRACT = Path(__file__).resolve().parents[2] / "schema_contract"
SNAPSHOT = CONTRACT / "drizzle_snapshot.json"
MIGRATIONS = CONTRACT / "migrations"
BREAKPOINT = "--> statement-breakpoint"


def admin_dsn() -> str | None:
    return os.environ.get("SCHEMA_TEST_DSN")


def _with_db(dsn: str, dbname: str) -> str:
    p = urlsplit(dsn)
    return urlunsplit((p.scheme, p.netloc, "/" + dbname, p.query, p.fragment))


def create_database(admin: str) -> tuple[str, callable]:
    """Crée une base vide ; retourne (dsn, fonction de nettoyage)."""
    import psycopg2

    name = f"schema_test_{uuid.uuid4().hex[:10]}"
    c = psycopg2.connect(admin)
    c.autocommit = True
    c.cursor().execute(f'CREATE DATABASE "{name}"')
    c.close()

    def drop() -> None:
        d = psycopg2.connect(admin)
        d.autocommit = True
        d.cursor().execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        d.close()

    return _with_db(admin, name), drop


def migration_files() -> list[Path]:
    journal = json.loads((MIGRATIONS / "_journal.json").read_text(encoding="utf-8"))
    return [MIGRATIONS / f"{e['tag']}.sql" for e in journal["entries"]]


def apply_migrations(dsn: str) -> None:
    """Rejoue les migrations officielles, dans l'ordre du journal, une transaction par fichier."""
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        for f in migration_files():
            stmts = [s.strip() for s in f.read_text(encoding="utf-8").split(BREAKPOINT) if s.strip()]
            with conn:
                with conn.cursor() as cur:
                    for s in stmts:
                        cur.execute(s)
    finally:
        conn.close()
