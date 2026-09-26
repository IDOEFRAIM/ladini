"""CLI de déploiement — remplace `alembic upgrade head` (jamais configuré,
voir `runner.py` pour l'incident complet) dans `scripts/cluster_deploy.sh`/
`scripts/node_deploy.sh`.

    python -m ladini.schema_migrations.cli

Lit `DATABASE_URL` (même variable que l'application). Sortie EXACTEMENT le
format demandé pour ne plus jamais annoncer "migrations appliquées" sans
preuve :

    migration source: drizzle schema_contract
    current: 0003_recurring_need_drafts
    pending: 0004_add_monthly_recurrence
    applied: 0004_add_monthly_recurrence

Code de sortie : 0 si le run s'est terminé sans erreur (y compris "rien à
faire"), 1 si `DATABASE_URL` est absent, 2 si une migration a échoué (le
process appelant — `dc run --rm`, voir cluster_deploy.sh — doit alors
considérer le déploiement comme NON réussi et ne PAS basculer le trafic)."""
from __future__ import annotations

import asyncio
import os
import sys

from ladini.schema_migrations.runner import MigrationFailure, apply_pending_migrations


def _dsn_from_env() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    # Le DSN applicatif porte parfois le dialecte SQLAlchemy
    # ("postgresql+asyncpg://") — asyncpg attend un DSN libpq nu.
    return raw.replace("postgresql+asyncpg://", "postgresql://", 1)


def main() -> int:
    dsn = _dsn_from_env()
    if not dsn:
        print("FATAL: DATABASE_URL n'est pas défini.", file=sys.stderr)
        return 1

    try:
        report = asyncio.run(apply_pending_migrations(dsn))
    except MigrationFailure as exc:
        for line in [
            "migration source: drizzle schema_contract",
            f"FATAL: migration {exc.tag!r} a échoué — AUCUNE migration suivante tentée, "
            "AUCUN rollout applicatif ne doit continuer.",
            f"cause: {type(exc.original).__name__}: {exc.original}",
        ]:
            print(line, file=sys.stderr)
        return 2

    for line in report.render_lines():
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
