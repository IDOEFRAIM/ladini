"""E2E ONLY — crée le schéma initial (Base.metadata.create_all) sur un
Postgres local jetable (docker-compose.e2e.yml), pour permettre de tester la
stack prod de bout en bout sans dépendre du Postgres managé réel.

Ce script n'existe PAS pour la production : le repo n'a aujourd'hui aucun
mécanisme de bootstrap de schéma reproductible (pas d'Alembic configuré, voir
docs/runbooks/migrations.md — "Aujourd'hui il n'y a pas d'Alembic : le schéma
initial est créé hors migration"). Le Postgres managé réel a été peuplé une
fois, manuellement, hors dépôt. Reproduire ce même schéma via
`Base.metadata.create_all()` est la seule façon fidèle de le recréer sans
toucher au code métier (les modèles ORM sont la source de vérité du schéma
tel qu'écrit dans le code, même s'ils n'ont jamais servi à le CRÉER en prod).

Usage :
    DATABASE_URL="postgresql+asyncpg://ladini_e2e:e2e_local_only@localhost:55432/ladini_e2e" \\
    backend/.venv/Scripts/python.exe scripts/test/e2e/bootstrap_db.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend", "src"))


async def main() -> None:
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://ladini_e2e:e2e_local_only@localhost:55432/ladini_e2e",
    )
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from ladini.domain.models import Base

    engine = create_async_engine(database_url, echo=False)
    async with engine.begin() as conn:
        # 4 schémas non-`public` référencés par les modèles ORM (voir
        # services/database/README.md §9 "Tables référencées") — jamais
        # créés automatiquement par create_all(), contrairement aux tables.
        for schema in ("auth", "marketplace", "governance", "intelligence"):
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        for ext in ("pgvector", "pg_trgm", "pgcrypto", "uuid-ossp"):
            # "pgvector" n'est pas un nom d'extension SQL valide (le paquet
            # s'appelle "vector") — normalisation minimale, cohérente avec
            # core/database.py::ensure_extensions().
            ext_name = "vector" if ext == "pgvector" else ext
            try:
                await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{ext_name}"'))
            except Exception as exc:  # pragma: no cover — diagnostic only
                print(f"[bootstrap_db] extension {ext_name} indisponible : {exc}")
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print(f"[bootstrap_db] schema created — {len(Base.metadata.tables)} tables")
    for table_name in sorted(Base.metadata.tables.keys()):
        print(f"  - {table_name}")


if __name__ == "__main__":
    asyncio.run(main())
