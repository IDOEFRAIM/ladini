"""`ladini.schema_migrations.runner` — le runner qui remplace la branche morte
`if [ -f backend/alembic.ini ]` dans `scripts/cluster_deploy.sh`/
`node_deploy.sh` (voir `runner.py` pour l'incident #1, 2026-09-26).

Incident #2 (2026-09-26, même jour) : la 1re version de ce runner inventait
un schéma de tracking (`id, tag, applied_at`) au lieu du format Drizzle RÉEL
déjà présent en production (`id, hash, created_at bigint`, `hash` = SHA256
du fichier). Les tests ci-dessous partent maintenant explicitement d'une
table de tracking pré-existante AU FORMAT RÉEL (jamais recréée par le
runner lui-même) — exactement ce que les tests précédents ne faisaient pas
(ils créaient toujours une base fraîche, donc le `CREATE TABLE IF NOT
EXISTS` du runner créait sa propre table et se comparait trivialement à
elle-même).

Contrairement à `test_recurring_supply.py::test_migration_0004_applies_in_
place_without_touching_existing_data` (qui rejoue le SQL brut à la main pour
prouver que 0004 lui-même est sûr), CE fichier teste le RUNNER."""
from __future__ import annotations

import json

import pytest
from psycopg2 import errors

pytestmark = pytest.mark.asyncio


def _require_admin_dsn():
    import db_tools

    admin = db_tools.admin_dsn()
    if not admin:
        pytest.skip("SCHEMA_TEST_DSN non défini — test PostgreSQL de migration ignoré en local.")
    return admin


def _seed_real_format_tracking_table(dsn, rows):
    """Crée `__drizzle_migrations` EXACTEMENT au format réel de production
    (`id, hash, created_at bigint`) et y insère `rows` (liste de
    `(hash, created_at)`) — jamais via le runner : ce module simule l'état
    DÉJÀ présent en base AVANT que le runner ne tourne, pour ne plus jamais
    laisser un test créer sa propre table de référence et la comparer à
    elle-même (root cause de l'incident #2)."""
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE __drizzle_migrations (id SERIAL PRIMARY KEY, hash TEXT NOT NULL, created_at BIGINT)"
        )
        for h, created_at in rows:
            cur.execute(
                "INSERT INTO __drizzle_migrations (hash, created_at) VALUES (%s, %s)",
                (h, created_at),
            )
    finally:
        conn.close()


def _real_migration_entries():
    """Les 5 migrations RÉELLES du repo (`schema_contract/migrations/`),
    hash SHA256 calculé par le runner lui-même — jamais une valeur inventée
    ici : si un fichier change, ce test le voit immédiatement."""
    from ladini.schema_migrations.runner import migration_entries

    return migration_entries()


async def test_a_prexisting_real_format_tracking_table_with_0000_to_0003_leaves_only_0004_pending():
    """LE scénario exact de l'incident #2 : `__drizzle_migrations` existe
    déjà, au format réel, avec 0000..0003 déjà trackées par leur hash —
    seule 0004 doit être détectée comme pending, jamais un rejeu de 0000..0003."""
    import db_tools
    import psycopg2

    from ladini.schema_migrations.runner import apply_pending_migrations

    admin = _require_admin_dsn()
    dsn, drop = db_tools.create_database(admin)
    try:
        entries = _real_migration_entries()
        already_applied = entries[:4]
        # Borné à 0004 (jamais `entries[4:]` brut) : ce test reproduit l'incident #2 EXACT, où
        # 0004 était la SEULE migration neuve — une migration ajoutée plus tard (ex: 0005) ne
        # doit pas faire dévier cette assertion de précondition (voir aussi `up_to_tag`
        # ci-dessous, qui borne le RUN lui-même de la même façon).
        still_pending = [e for e in entries[4:] if e.tag == "0004_add_monthly_recurrence"]
        assert [e.tag for e in still_pending] == ["0004_add_monthly_recurrence"]

        # 1) Applique 0000..0003 au SQL brut (jamais via le runner — on ne
        # veut PAS que le runner écrive lui-même la ligne de tracking ici,
        # sinon ce test ne prouverait rien sur le format qu'IL produit).
        conn = psycopg2.connect(dsn)
        try:
            with conn:
                with conn.cursor() as cur:
                    for e in already_applied:
                        for stmt in (
                            s.strip() for s in e.path.read_text(encoding="utf-8").split("--> statement-breakpoint")
                        ):
                            if stmt:
                                cur.execute(stmt)
        finally:
            conn.close()

        # 2) Table de tracking RÉELLE, pré-existante, format réel de prod.
        _seed_real_format_tracking_table(
            dsn, [(e.hash, e.when) for e in already_applied]
        )

        # MONTHLY doit encore être refusé (base réellement à 0003).
        conn = psycopg2.connect(dsn)
        try:
            from factories import Graph

            cur = conn.cursor()
            g = Graph(cur)
            conn.commit()
            with pytest.raises(errors.CheckViolation):
                g.recurring_need(recurrence_type="MONTHLY")
            conn.rollback()
        finally:
            conn.close()

        # 3) Le runner ne doit voir QUE 0004 en attente. `up_to_tag` borne ce test à
        # l'incident #2 EXACT (0004 seule migration neuve à l'époque) — sans lui, une
        # migration ajoutée plus tard (ex: 0005) serait AUSSI candidate, ce qui ne
        # reproduirait plus le scénario historique précis que ce test verrouille.
        report = await apply_pending_migrations(dsn, up_to_tag="0004_add_monthly_recurrence")
        assert report.tracked_count_before == 4
        assert report.current_before == "0003_recurring_need_drafts"
        assert report.pending == ["0004_add_monthly_recurrence"]
        assert report.applied == ["0004_add_monthly_recurrence"]

        # MONTHLY accepté après.
        conn = psycopg2.connect(dsn)
        try:
            from factories import Graph

            cur = conn.cursor()
            g = Graph(cur)
            conn.commit()
            g.recurring_need(recurrence_type="MONTHLY")
            conn.commit()
        finally:
            conn.close()

        # Ligne de tracking pour 0004 ajoutée, format réel respecté, jamais
        # de colonne `tag` (le format historique n'est pas modifié).
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("select column_name from information_schema.columns where table_name = '__drizzle_migrations'")
            columns = {r[0] for r in cur.fetchall()}
            assert columns == {"id", "hash", "created_at"}, columns

            cur.execute("select hash, created_at from __drizzle_migrations order by id")
            rows = cur.fetchall()
            assert len(rows) == 5
            assert rows[-1] == (
                still_pending[0].hash,
                still_pending[0].when,
            )
        finally:
            conn.close()

        # 4) Deuxième run (même borne) : plus rien en attente, aucun doublon.
        second = await apply_pending_migrations(dsn, up_to_tag="0004_add_monthly_recurrence")
        assert second.tracked_count_before == 5
        assert second.pending == []
        assert second.applied == []
        assert second.current_before == "0004_add_monthly_recurrence"

        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("select count(*) from __drizzle_migrations")
            assert cur.fetchone()[0] == 5, "aucun doublon attendu"
        finally:
            conn.close()
    finally:
        drop()


async def test_bootstrap_from_an_empty_db_with_no_tracking_table_applies_everything():
    """Mandat §6.C : table de tracking ABSENTE + DB vide -> bootstrap complet de TOUTES les
    migrations connues du journal local, le runner crée la table au format réel (jamais
    `tag`). Compte/dernière migration lus DYNAMIQUEMENT (`_real_migration_entries()`) — jamais
    un nombre en dur, pour ne plus jamais avoir à toucher ce test au prochain ajout."""
    import db_tools
    import psycopg2

    from ladini.schema_migrations.runner import apply_pending_migrations

    admin = _require_admin_dsn()
    dsn, drop = db_tools.create_database(admin)
    try:
        entries = _real_migration_entries()
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "select 1 from information_schema.tables where table_name = '__drizzle_migrations'"
            )
            assert cur.fetchone() is None, "précondition : pas de table de tracking avant le run"
        finally:
            conn.close()

        report = await apply_pending_migrations(dsn)
        assert report.tracked_count_before == 0
        assert report.current_before is None
        assert len(report.applied) == len(entries)
        assert report.applied[-1] == entries[-1].tag

        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "select column_name from information_schema.columns where table_name = '__drizzle_migrations'"
            )
            assert {r[0] for r in cur.fetchall()} == {"id", "hash", "created_at"}
        finally:
            conn.close()
    finally:
        drop()


async def test_a_hash_in_tracking_unknown_to_the_local_journal_is_a_hard_failure():
    """Mandat §6.A : la base "sait" avoir appliqué plus de migrations que le
    journal local n'en connaît — jamais silencieux, jamais une tentative de
    deviner ce qu'il faudrait faire."""
    import db_tools

    from ladini.schema_migrations.runner import MigrationDrift, apply_pending_migrations

    admin = _require_admin_dsn()
    dsn, drop = db_tools.create_database(admin)
    try:
        entries = _real_migration_entries()
        # 6 lignes trackées pour seulement 5 migrations connues du journal —
        # la 6e ("fantôme") ne correspond à AUCUN fichier local.
        _seed_real_format_tracking_table(
            dsn,
            [(e.hash, e.when) for e in entries] + [("f" * 64, 999999999999)],
        )

        with pytest.raises(MigrationDrift):
            await apply_pending_migrations(dsn)
    finally:
        drop()


async def test_a_changed_historical_migration_hash_is_a_hard_failure():
    """Mandat §6.B : le hash trackée pour une migration déjà appliquée ne
    correspond plus au fichier local (fichier modifié après coup) -> drift,
    jamais une tentative de réappliquer à l'aveugle."""
    import db_tools

    from ladini.schema_migrations.runner import MigrationDrift, apply_pending_migrations

    admin = _require_admin_dsn()
    dsn, drop = db_tools.create_database(admin)
    try:
        entries = _real_migration_entries()
        rows = [(e.hash, e.when) for e in entries[:4]]
        # Corrompt le hash de la 3e migration déjà "appliquée" (0002) — un
        # hash différent de ce que le fichier local calcule aujourd'hui.
        rows[2] = ("0" * 64, rows[2][1])
        _seed_real_format_tracking_table(dsn, rows)

        with pytest.raises(MigrationDrift):
            await apply_pending_migrations(dsn)
    finally:
        drop()


async def test_a_broken_migration_stops_the_run_and_leaves_no_trace(tmp_path):
    """Migration cassée -> `MigrationFailure`, rien n'est enregistré pour
    elle (le prochain run la retenterait, jamais un rejeu silencieux d'une
    migration `réussie`), et — point central du mandat — le process
    appelant (`scripts/*.sh`, via le CLI) doit voir un échec net, jamais un
    "migrations appliquées" mensonger."""
    import db_tools
    import psycopg2

    from ladini.schema_migrations.runner import (
        MigrationFailure,
        apply_pending_migrations,
    )

    admin = _require_admin_dsn()
    dsn, drop = db_tools.create_database(admin)
    try:
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "0000_ok.sql").write_text(
            "CREATE TABLE ok_table (id int primary key);", encoding="utf-8"
        )
        (migrations_dir / "0001_broken.sql").write_text(
            "CREATE TABLE this is not valid sql;", encoding="utf-8"
        )
        (migrations_dir / "_journal.json").write_text(
            json.dumps(
                {
                    "version": "7",
                    "dialect": "postgresql",
                    "entries": [
                        {"idx": 0, "version": "7", "when": 1, "tag": "0000_ok"},
                        {"idx": 1, "version": "7", "when": 2, "tag": "0001_broken"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(MigrationFailure) as exc_info:
            await apply_pending_migrations(dsn, migrations_dir=migrations_dir)
        assert exc_info.value.tag == "0001_broken"

        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("select count(*) from __drizzle_migrations")
            # 0000_ok est bien passée (sa propre transaction a commité) ;
            # 0001_broken n'a JAMAIS été marquée appliquée.
            assert cur.fetchone()[0] == 1

            cur.execute(
                "select table_name from information_schema.tables where table_name = 'ok_table'"
            )
            assert cur.fetchone() is not None
        finally:
            conn.close()

        # Rejouer ne retente QUE 0001_broken (0000_ok n'est jamais rejouée).
        with pytest.raises(MigrationFailure) as exc_info_2:
            await apply_pending_migrations(dsn, migrations_dir=migrations_dir)
        assert exc_info_2.value.tag == "0001_broken"
    finally:
        drop()
