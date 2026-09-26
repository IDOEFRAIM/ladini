"""Runner de migrations Drizzle — SEUL mécanisme officiel d'application du
schéma (voir `backend/schema_contract/migrations/`, `scripts/check_migrations.sh`
en tête de fichier : "Drizzle définit et migre ; le backend n'exécute plus
aucun DDL").

Incident réel (2026-09-26) : `scripts/cluster_deploy.sh`/`node_deploy.sh`
classifiaient déjà chaque migration (`check_migrations.sh`, EXPAND/CONTRACT)
mais l'étape d'APPLICATION réelle était entièrement gardée par
`if [ -f backend/alembic.ini ]` — fichier qui n'existe nulle part dans ce
repo. Résultat : la classification passait, le déploiement affichait
"migrations appliquées" (branche succès du `if`)... sans jamais exécuter la
moindre migration Drizzle, sur AUCUNE release depuis la mise en place de ce
pipeline. `backend/schema_contract/migrations/0004_add_monthly_recurrence.sql`
était présent sur le node, référencé dans `_journal.json`, mais jamais
rejoué contre la vraie base — d'où le `CHECK recurrence_type_chk` resté sans
MONTHLY en prod malgré un code applicatif qui l'acceptait déjà.

Ce module remplace la branche `alembic upgrade head` par un runner qui lit
RÉELLEMENT `_journal.json` et applique les migrations non encore
enregistrées. Réutilisé par :
  - `scripts/deploy_migrate.py` (CLI appelée depuis cluster_deploy.sh/node_deploy.sh)
  - `backend/tests/schema/db_tools.py` (tests de schéma — devient un fin
    wrapper autour de `apply_pending_migrations`, plus un second moteur)
  - `scripts/test/e2e/bootstrap_db.py` (bootstrap E2E)

Connexion : `asyncpg` DIRECT (pas SQLAlchemy), `statement_cache_size=0`.
`asyncpg` est déjà une dépendance de PRODUCTION (contrairement à
`psycopg2-binary`, dev-only — voir `pyproject.toml`, jamais dans l'image
`api`) — le runner doit pouvoir tourner DANS ce même conteneur. `statement_
cache_size=0` désactive le cache de prepared statements côté client : sans
ça, PgBouncer en mode transaction (une connexion serveur différente à
chaque transaction côté pool) fait remonter `DuplicatePreparedStatementError`
dès qu'asyncpg réutilise un statement préparé sur une session serveur qui ne
le connaît plus — exactement l'incident observé avec un script ad hoc. Une
migration (DDL, `BEGIN; ...; COMMIT;`) n'a de toute façon aucun besoin de
prepared statements : ce réglage n'a aucun coût ici.

Aucune ligne de ce module ne doit jamais logger un DSN (peut contenir un mot
de passe) — seul le tag de migration / le nombre de statements peut
apparaître dans les logs."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BREAKPOINT = "--> statement-breakpoint"
TRACKING_TABLE = "__drizzle_migrations"

# backend/src/ladini/schema_migrations/runner.py -> backend/schema_contract/migrations
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "schema_contract" / "migrations"


@dataclass(frozen=True)
class MigrationEntry:
    idx: int
    tag: str
    path: Path


@dataclass
class MigrationRunReport:
    """Ce que le CLI affiche — voir le format exact demandé (mandat §7) :

        migration source: drizzle schema_contract
        current: <tag ou "none">
        pending: <tags séparés par ", " ou "none">
        applied: <tags séparés par ", " ou "none">
    """

    current_before: Optional[str]
    pending: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    def render_lines(self) -> list[str]:
        return [
            "migration source: drizzle schema_contract",
            f"current: {self.current_before or 'none'}",
            f"pending: {', '.join(self.pending) if self.pending else 'none'}",
            f"applied: {', '.join(self.applied) if self.applied else 'none'}",
        ]


class MigrationFailure(RuntimeError):
    """Levée quand UNE migration échoue — le runner s'arrête IMMÉDIATEMENT,
    ne tente aucune migration suivante, et ne marque RIEN comme appliqué
    pour celle qui a échoué (sa transaction est annulée automatiquement par
    `asyncpg`, `__drizzle_migrations` n'est jamais mise à jour pour elle :
    le prochain run la retentera, jamais un rejeu silencieux d'une migration
    déjà réussie)."""

    def __init__(self, tag: str, original: BaseException) -> None:
        super().__init__(f"migration {tag!r} a échoué : {type(original).__name__}: {original}")
        self.tag = tag
        self.original = original


def migration_entries(migrations_dir: Optional[Path] = None) -> list[MigrationEntry]:
    """Lit `_journal.json` — SOURCE UNIQUE de l'ordre canonique des
    migrations (jamais un tri par nom de fichier : les tags ne sont pas
    garantis lexicographiquement ordonnés au-delà de 9999)."""
    directory = migrations_dir or _DEFAULT_MIGRATIONS_DIR
    journal = json.loads((directory / "_journal.json").read_text(encoding="utf-8"))
    entries = [
        MigrationEntry(idx=e["idx"], tag=e["tag"], path=directory / f"{e['tag']}.sql")
        for e in journal["entries"]
    ]
    entries.sort(key=lambda e: e.idx)
    return entries


def _split_statements(sql_text: str) -> list[str]:
    return [s.strip() for s in sql_text.split(BREAKPOINT) if s.strip()]


async def apply_pending_migrations(
    dsn: str,
    *,
    migrations_dir: Optional[Path] = None,
    up_to_tag: Optional[str] = None,
) -> MigrationRunReport:
    """Applique les migrations Drizzle non encore enregistrées, DANS L'ORDRE
    du journal, UNE TRANSACTION PAR FICHIER. Idempotent : un second appel
    sans nouvelle migration ne fait rien (`pending`/`applied` vides).

    `up_to_tag` (tests uniquement) : ignore toute entrée du journal
    POSTÉRIEURE à ce tag — permet de rejouer "la base au moment de la
    release N" sans dupliquer un jeu de fichiers de migration séparé.

    Échoue fort : la première migration qui lève une exception interrompt
    le run (`MigrationFailure`, cause originale chaînée) — jamais de
    tentative sur la suivante, jamais de `__drizzle_migrations` mis à jour
    pour la migration en échec."""
    import asyncpg

    all_entries = migration_entries(migrations_dir)
    if up_to_tag is not None:
        cutoff = next((e.idx for e in all_entries if e.tag == up_to_tag), None)
        if cutoff is None:
            raise ValueError(f"up_to_tag={up_to_tag!r} introuvable dans le journal.")
        all_entries = [e for e in all_entries if e.idx <= cutoff]

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
                id BIGSERIAL PRIMARY KEY,
                tag TEXT NOT NULL UNIQUE,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied_rows = await conn.fetch(f"SELECT tag FROM {TRACKING_TABLE}")
        applied_tags = {r["tag"] for r in applied_rows}

        current_before = None
        for entry in reversed(all_entries):
            if entry.tag in applied_tags:
                current_before = entry.tag
                break

        pending = [e for e in all_entries if e.tag not in applied_tags]
        report = MigrationRunReport(
            current_before=current_before, pending=[e.tag for e in pending]
        )

        for entry in pending:
            statements = _split_statements(entry.path.read_text(encoding="utf-8"))
            try:
                async with conn.transaction():
                    for stmt in statements:
                        await conn.execute(stmt)
                    await conn.execute(
                        f"INSERT INTO {TRACKING_TABLE} (tag) VALUES ($1)", entry.tag
                    )
            except Exception as exc:  # noqa: BLE001 - on veut tout capturer et arrêter net
                raise MigrationFailure(entry.tag, exc) from exc
            report.applied.append(entry.tag)

        return report
    finally:
        await conn.close()


__all__ = [
    "BREAKPOINT",
    "TRACKING_TABLE",
    "MigrationEntry",
    "MigrationFailure",
    "MigrationRunReport",
    "apply_pending_migrations",
    "migration_entries",
]
