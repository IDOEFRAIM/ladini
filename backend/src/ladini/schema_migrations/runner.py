"""Runner de migrations Drizzle — SEUL mécanisme officiel d'application du
schéma (voir `backend/schema_contract/migrations/`, `scripts/check_migrations.sh`
en tête de fichier : "Drizzle définit et migre ; le backend n'exécute plus
aucun DDL").

Incident réel #1 (2026-09-26) : `scripts/cluster_deploy.sh`/`node_deploy.sh`
classifiaient déjà chaque migration mais l'étape d'APPLICATION réelle était
entièrement gardée par `if [ -f backend/alembic.ini ]` — fichier qui
n'existe nulle part dans ce repo. Aucune migration Drizzle n'était jamais
réellement appliquée. Voir git blame de ce fichier pour le détail complet.

Incident réel #2 (2026-09-26, quelques heures après le déploiement du
correctif #1) : le premier jet de ce runner INVENTAIT le schéma de la table
de suivi (`id, tag, applied_at`) au lieu de vérifier le format RÉELLEMENT
déjà présent en production. `public.__drizzle_migrations` existait déjà
(bootstrap initial du projet, vraisemblablement via `drizzle-kit` en dehors
de ce pipeline cassé) avec le format CANONIQUE Drizzle :

    __drizzle_migrations(id, hash text, created_at bigint)

— `hash` = SHA256 hexdigest du contenu BRUT du fichier `.sql` (jamais du tag
ni d'un identifiant inventé), `created_at` = le timestamp `when` DÉJÀ figé
dans `_journal.json` pour cette migration (PAS l'horodatage réel de
l'application — c'est un détail de convention Drizzle, pas une erreur : le
même fichier a toujours le même `hash`/`created_at`, peu importe QUAND ni
COMBIEN de fois on "découvre" qu'il faut l'appliquer).

Tous les tests de la 1re version créaient une base fraîche à chaque fois
(`db_tools.create_database`), donc le `CREATE TABLE IF NOT EXISTS` de CE
runner créait toujours SA PROPRE table, qui se comparait alors
trivialement à elle-même — aucun test ne simulait "une base qui a DÉJÀ une
vraie table Drizzle, avec un format différent de celui supposé ici". Voir
`tests/schema/test_migration_runner.py` pour les tests qui couvrent
maintenant explicitement ce cas (table pré-existante au format réel,
détection de drift si le hash d'un fichier déjà appliqué a changé).

Correspondance migration <-> ligne de tracking : PAR HASH, jamais par tag
(`tag` n'existe pas dans le format réel, il ne sert qu'à l'affichage/logs,
lu depuis `_journal.json` en mémoire). L'algorithme suit l'hypothèse de
Drizzle lui-même — les migrations sont TOUJOURS appliquées dans l'ordre du
journal, sans trou : les N premières entrées du journal correspondent aux N
lignes déjà trackées, dans le même ordre. Si le hash d'une entrée déjà
"couverte" par ce préfixe ne correspond PAS à la ligne trackée à la même
position, c'est soit un fichier de migration modifié APRÈS coup (jamais
acceptable), soit un tracking plus avancé que le checkout local (déploiement
d'un commit en arrière) — dans les deux cas : `MigrationDrift`, jamais une
tentative de réappliquer/deviner.

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
de passe) — seuls tag/hash tronqué/nombre de statements peuvent apparaître
dans les logs."""
from __future__ import annotations

import hashlib
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
    when: int
    path: Path
    hash: str


@dataclass
class MigrationRunReport:
    """Ce que le CLI affiche — voir le format exact demandé (mandat §9) :

        migration source: drizzle schema_contract
        tracking table: public.__drizzle_migrations
        applied hashes: <n>
        current: <tag ou "none">
        pending: <tags séparés par ", " ou "none">
        applied: <tags séparés par ", " ou "none">
    """

    tracked_count_before: int
    current_before: Optional[str]
    pending: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    def render_lines(self) -> list[str]:
        return [
            "migration source: drizzle schema_contract",
            f"tracking table: public.{TRACKING_TABLE}",
            f"applied hashes: {self.tracked_count_before}",
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


class MigrationDrift(RuntimeError):
    """Le préfixe déjà trackée en base ne correspond PAS, hash par hash et
    dans l'ordre, au début du journal local — jamais silencieux, jamais une
    tentative de deviner/réappliquer. Deux causes possibles, toutes deux
    graves : (1) un fichier de migration déjà appliqué a été modifié après
    coup (son hash a changé) ; (2) le tracking en base est plus avancé que
    ce que journal/checkout local connaissent (déploiement d'un commit en
    arrière, ou fichier de migration supprimé localement)."""


def _sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def migration_entries(migrations_dir: Optional[Path] = None) -> list[MigrationEntry]:
    """Lit `_journal.json` — SOURCE UNIQUE de l'ordre canonique des
    migrations (jamais un tri par nom de fichier : les tags ne sont pas
    garantis lexicographiquement ordonnés au-delà de 9999). Calcule le hash
    SHA256 de chaque fichier — LA clé de correspondance avec le tracking
    réel (`hash`, jamais `tag`, voir docstring de module)."""
    directory = migrations_dir or _DEFAULT_MIGRATIONS_DIR
    journal = json.loads((directory / "_journal.json").read_text(encoding="utf-8"))
    entries = [
        MigrationEntry(
            idx=e["idx"],
            tag=e["tag"],
            when=e["when"],
            path=(path := directory / f"{e['tag']}.sql"),
            hash=_sha256_hex(path),
        )
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
    pour la migration en échec. Un préfixe trackée incohérent avec le
    journal local lève `MigrationDrift` AVANT toute tentative d'application
    (voir sa docstring)."""
    import asyncpg

    all_entries = migration_entries(migrations_dir)
    if up_to_tag is not None:
        cutoff = next((e.idx for e in all_entries if e.tag == up_to_tag), None)
        if cutoff is None:
            raise ValueError(f"up_to_tag={up_to_tag!r} introuvable dans le journal.")
        all_entries = [e for e in all_entries if e.idx <= cutoff]

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        # Format CANONIQUE Drizzle réel — voir docstring de module pour
        # l'incident qui a suivi la première version de ce runner (qui
        # inventait une colonne `tag`). Aucune contrainte UNIQUE sur `hash`
        # ici : le format historique observé en production n'en porte pas,
        # et ce runner ne doit RIEN ajouter au schéma réel (mandat §5) — la
        # détection de doublon/drift se fait en Python, pas en base.
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
                id SERIAL PRIMARY KEY,
                hash TEXT NOT NULL,
                created_at BIGINT
            )
            """
        )
        tracked_rows = await conn.fetch(f"SELECT hash FROM {TRACKING_TABLE} ORDER BY id")
        tracked_hashes = [r["hash"] for r in tracked_rows]

        if len(tracked_hashes) > len(all_entries):
            raise MigrationDrift(
                f"{TRACKING_TABLE} porte {len(tracked_hashes)} migration(s) déjà appliquée(s), "
                f"mais le journal local n'en connaît que {len(all_entries)} — checkout local "
                "en retard sur la base réelle (commit antérieur déployé par erreur ?)."
            )
        # strict=False DÉLIBÉRÉ : `all_entries` est normalement plus long que
        # `tracked_hashes` (c'est exactement la définition de "pending") —
        # `zip` tronque volontairement sur le plus court, la longueur a déjà
        # été validée juste au-dessus (`tracked_hashes` jamais plus long).
        for position, (entry, tracked_hash) in enumerate(
            zip(all_entries, tracked_hashes, strict=False)
        ):
            if entry.hash != tracked_hash:
                raise MigrationDrift(
                    f"position {position} : {TRACKING_TABLE} porte le hash {tracked_hash[:12]}… "
                    f"mais le fichier local {entry.tag!r} calcule {entry.hash[:12]}… — fichier de "
                    "migration modifié après application, ou tracking désynchronisé du journal."
                )

        current_before = all_entries[len(tracked_hashes) - 1].tag if tracked_hashes else None
        pending = all_entries[len(tracked_hashes) :]
        report = MigrationRunReport(
            tracked_count_before=len(tracked_hashes),
            current_before=current_before,
            pending=[e.tag for e in pending],
        )

        for entry in pending:
            statements = _split_statements(entry.path.read_text(encoding="utf-8"))
            try:
                async with conn.transaction():
                    for stmt in statements:
                        await conn.execute(stmt)
                    await conn.execute(
                        f"INSERT INTO {TRACKING_TABLE} (hash, created_at) VALUES ($1, $2)",
                        entry.hash,
                        entry.when,
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
    "MigrationDrift",
    "MigrationEntry",
    "MigrationFailure",
    "MigrationRunReport",
    "apply_pending_migrations",
    "migration_entries",
]
