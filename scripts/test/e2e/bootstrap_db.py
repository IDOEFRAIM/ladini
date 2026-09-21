"""E2E ONLY — construit le schéma d'un Postgres local jetable (docker-compose.e2e.yml) en rejouant
les MIGRATIONS OFFICIELLES (Drizzle, copie versionnée `backend/schema_contract/migrations/`).

Le schéma n'est PLUS jamais créé depuis les modèles ORM (`Base.metadata.create_all` supprimé) : c'est
exactement le chemin de production (Drizzle -> PostgreSQL), donc l'E2E teste le vrai schéma.

Usage :
    DATABASE_URL="postgresql://ladini_e2e:e2e_local_only@localhost:55432/ladini_e2e" \\
    python scripts/test/e2e/bootstrap_db.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, os.path.join(ROOT, "backend", "tests", "schema"))


def main() -> None:
    import db_tools  # noqa: PLC0415 — après sys.path

    url = os.environ.get("DATABASE_URL", "postgresql://ladini_e2e:e2e_local_only@localhost:55432/ladini_e2e")
    # Le DSN de l'appli peut porter le driver SQLAlchemy (+asyncpg) ; psycopg2 attend un DSN libpq.
    dsn = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    db_tools.apply_migrations(dsn)
    print(f"[bootstrap_db] migrations appliquées : {', '.join(f.name for f in db_tools.migration_files())}")


if __name__ == "__main__":
    main()
