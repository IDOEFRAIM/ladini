"""Cohérence de schéma SANS base de données : SQLAlchemy <-> contrat Drizzle (source de vérité).

Échoue avec un rapport lisible (table / colonne / aspect / valeur par source).
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

import db_tools
import pytest
import schema_model as m
from schema_policy import SITE_ONLY_TABLES

SRC = Path(__file__).resolve().parents[2] / "src" / "ladini"


def _fail_if(divs, title):
    if divs:
        pytest.fail("\n" + m.render(divs, title), pytrace=False)


def test_sqlalchemy_mirrors_drizzle_exactly(drizzle_schema, sqlalchemy_schema):
    """Tables, colonnes, types, nullabilité, défauts, PK, FK (+ON DELETE/UPDATE), uniques, index."""
    divs = m.compare(drizzle_schema, sqlalchemy_schema, "drizzle", "sqlalchemy", ignore_tables=SITE_ONLY_TABLES)
    _fail_if(divs, "SQLAlchemy != Drizzle")


def test_site_only_tables_are_declared_in_drizzle_and_absent_from_sqlalchemy(drizzle_schema, sqlalchemy_schema):
    for t in SITE_ONLY_TABLES:
        assert t in drizzle_schema, f"{t} déclarée site-only mais absente de Drizzle"
        assert t not in sqlalchemy_schema, f"{t} déclarée site-only mais un modèle Python existe : retirer de SITE_ONLY_TABLES"


def test_abandoned_memory_tables_are_gone(drizzle_schema, sqlalchemy_schema):
    banned = {("public", "episodic_memories"), ("public", "user_farm_profiles")}
    assert not (banned & set(drizzle_schema)), "table mémoire abandonnée réapparue dans Drizzle"
    assert not (banned & set(sqlalchemy_schema)), "modèle mémoire abandonné réapparu dans SQLAlchemy"


def test_no_reference_to_abandoned_memory_feature_in_source():
    pat = re.compile(r"episodic_memor|EpisodicMemor|user_farm_profile|UserFarmProfile|ContextOptimizer|ProfileExtractor")
    hits = [
        f"{p.relative_to(SRC)}:{i}"
        for p in SRC.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1)
        if pat.search(line)
    ]
    assert not hits, "références résiduelles à la fonctionnalité mémoire abandonnée :\n  " + "\n  ".join(hits)


# Une FK dont la colonne n'a pas d'index de tête est tolérée UNIQUEMENT ici, avec justification.
FK_WITHOUT_INDEX_OK: dict[tuple[str, str, str], str] = {
    ("governance", "standard_prices", "updated_by_id"): "table de référence minuscule ; simple audit de l'éditeur",
    ("governance", "user_organizations", "managed_zone_id"): "table d'appartenance minuscule, jamais filtrée par zone gérée",
    ("governance", "work_zones", "manager_id"): "table d'affectation minuscule",
    ("marketplace", "auctions", "winner_bid_id"): "FK circulaire ; les offres ne sont jamais supprimées (RESTRICT) et on accède au gagnant par sa PK",
    ("marketplace", "seed_allocations", "allocated_by_id"): "table site à faible volume",
    ("marketplace", "seed_distributions", "zone_id"): "table site à faible volume",
    ("intelligence", "conversations", "zone_id"): "jamais filtrée par zone ; SET NULL très rare",
    ("intelligence", "solicitations", "target_producer_id"): "jamais filtrée par cette colonne (requêtes par auction/offre/kind+status) ; suppression parent rare",
    ("intelligence", "solicitations", "target_buyer_id"): "idem target_producer_id",
    ("intelligence", "solicitations", "sub_category_id"): "information de ciblage, jamais filtrée",
    ("intelligence", "solicitations", "zone_id"): "information de ciblage, jamais filtrée",
    ("intelligence", "notification_outbox", "recipient_user_id"): "la file est lue par (status, next_attempt_at) ; suppression d'utilisateur = opération rare",
    ("intelligence", "demand_signals", "user_id"): "signal agrégé par terme normalisé (unique) ; jamais filtré par utilisateur",
    ("intelligence", "demand_signals", "zone_id"): "idem user_id",
    ("marketplace", "buyer_profiles", "verified_by_id"): "audit de vérification ; suppression d'admin = opération rare",
    ("marketplace", "seed_distribution_attempts", "actor_id"): "table site à faible volume",
    ("analytics", "business_events", "producer_id"): "table analytics append-only à fort volume d'écriture ; jamais filtrée par producteur en Phase C (Phase D indexera si une requête l'exige) ; suppression parent (SET NULL) très rare",
    ("analytics", "business_events", "category_id"): "idem producer_id ; les catégories ne sont pratiquement jamais supprimées",
}


def test_every_foreign_key_column_is_indexed(drizzle_schema):
    """Chaque FK doit avoir un index (ou PK/unique) dont la 1re colonne est la colonne FK : sans lui,
    les jointures et les ON DELETE = scan séquentiel de la table enfant."""
    missing = []
    for (s, t), tb in drizzle_schema.items():
        leading = {tb.pk[:1]} if tb.pk else set()
        leading |= {u[:1] for u in tb.uniques} | {i.cols[:1] for i in tb.indexes.values()}
        for cols in tb.fks:
            if cols[:1] not in leading and (s, t, cols[0]) not in FK_WITHOUT_INDEX_OK:
                missing.append(f"{s}.{t}({','.join(cols)}) -> {'.'.join(tb.fks[cols].ref)}")
    assert not missing, (
        "FK sans index (ajouter un index dans Drizzle ou justifier dans FK_WITHOUT_INDEX_OK) :\n  " + "\n  ".join(missing)
    )


def test_no_runtime_ddl_in_backend_source():
    """Le backend ne crée/modifie JAMAIS le schéma : ni create_all, ni CREATE/ALTER/DROP dans une chaîne.
    Le schéma vient exclusivement des migrations Drizzle.

    UNE exception sanctionnée (2026-09-26, incident migration delivery) :
    `schema_migrations/runner.py` — LE runner qui APPLIQUE les migrations
    Drizzle — auto-crée sa propre table de suivi (`__drizzle_migrations`,
    `CREATE TABLE IF NOT EXISTS`) au premier lancement contre une base
    vierge. Ce n'est pas "le backend qui mute son schéma métier au runtime"
    (ce que cette règle interdit) : c'est l'outil de migration lui-même qui
    pose SES PROPRES métadonnées de suivi — exactement ce que fait tout
    outil de migration (Alembic, Flyway, et `drizzle-kit` lui-même en
    interne) avant de pouvoir lire "qu'est-ce qui est déjà appliqué ?".
    Cette table est déjà exclue des comparaisons de schéma (voir
    `conftest.py::pg_schema`, `k[1] != "__drizzle_migrations"`)."""
    ddl = re.compile(r"^\s*(CREATE|ALTER|DROP)\s+(UNIQUE\s+)?(TABLE|INDEX|EXTENSION|SCHEMA|TYPE|SEQUENCE)\b", re.I)
    MIGRATION_RUNNER_BOOTSTRAP = SRC / "schema_migrations" / "runner.py"
    offenders = []
    for p in SRC.rglob("*.py"):
        if p == MIGRATION_RUNNER_BOOTSTRAP:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in {"create_all", "drop_all"}:
                offenders.append(f"{p.relative_to(SRC)}:{n.lineno} {n.func.attr}()")
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and ddl.match(n.value):
                offenders.append(f"{p.relative_to(SRC)}:{n.lineno} DDL: {n.value.strip()[:50]}")
    assert not offenders, "DDL au runtime interdit — passer par une migration Drizzle :\n  " + "\n  ".join(offenders)


def test_no_postgres_enum_types_yet():
    """Aucun enum PG aujourd'hui (statuts = text). Si un enum apparaît dans Drizzle, ce test force à ajouter
    le miroir SQLAlchemy et à le comparer, au lieu de laisser dériver."""
    enums = json.loads(db_tools.SNAPSHOT.read_text(encoding="utf-8")).get("enums", {})
    assert not enums, f"enum(s) Drizzle {list(enums)} : ajouter le miroir SQLAlchemy Enum(...) et le comparer"


@pytest.mark.skipif(
    not (os.environ.get("LADINI_FRONTEND_DIR") and (Path(os.environ["LADINI_FRONTEND_DIR"]) / "drizzle").exists()),
    reason="dépôt frontend non disponible (LADINI_FRONTEND_DIR) — contrôle de synchronisation ignoré",
)
def test_contract_copy_is_in_sync_with_frontend_repo():
    from sync_contract import CONTRACT, _sources

    def norm(b: bytes) -> bytes:
        return b.replace(b"\r\n", b"\n")

    stale = [
        rel
        for rel, path in _sources(Path(os.environ["LADINI_FRONTEND_DIR"])).items()
        if not (CONTRACT / rel).exists() or norm((CONTRACT / rel).read_bytes()) != norm(path.read_bytes())
    ]
    assert not stale, "schema_contract périmé — python tests/schema/sync_contract.py --frontend <repo> :\n  " + "\n  ".join(stale)
