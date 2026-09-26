"""`ladini.schema_migrations.runner` — le runner qui remplace la branche morte
`if [ -f backend/alembic.ini ]` dans `scripts/cluster_deploy.sh`/
`node_deploy.sh` (voir `runner.py` pour l'incident complet, 2026-09-26).

Contrairement à `test_recurring_supply.py::test_migration_0004_applies_in_
place_without_touching_existing_data` (qui rejoue le SQL brut à la main pour
prouver que 0004 lui-même est sûr), CE fichier teste le RUNNER — l'étape de
déploiement qui, avant ce correctif, n'était jamais invoquée du tout."""
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


async def test_fresh_db_applies_only_up_to_0003_then_0004_separately():
    """Reproduit exactement le scénario du mandat §6 : DB à 0003, puis release
    contenant 0004 -> le runner applique SEULEMENT ce qui manque."""
    import db_tools

    from ladini.schema_migrations.runner import apply_pending_migrations

    admin = _require_admin_dsn()
    dsn, drop = db_tools.create_database(admin)
    try:
        report_1 = await apply_pending_migrations(
            dsn, up_to_tag="0003_recurring_need_drafts"
        )
        assert report_1.current_before is None
        assert report_1.pending == [
            "0000_baseline",
            "0001_agent_telemetry",
            "0002_recurring_supply",
            "0003_recurring_need_drafts",
        ]
        assert report_1.applied == report_1.pending

        # MONTHLY doit encore être refusé par le CHECK à ce stade (voir aussi
        # test_recurring_supply.py::test_migration_0004_applies_in_place_...
        # pour la couverture détaillée de ce CHECK) — confirme qu'on est
        # bien parti d'une base réellement à 0003, pas déjà à 0004.
        import psycopg2
        from factories import Graph

        conn = psycopg2.connect(dsn)
        try:
            cur = conn.cursor()
            g = Graph(cur)
            conn.commit()
            with pytest.raises(errors.CheckViolation):
                g.recurring_need(recurrence_type="MONTHLY")
            conn.rollback()
        finally:
            conn.close()

        report_2 = await apply_pending_migrations(dsn)
        assert report_2.current_before == "0003_recurring_need_drafts"
        assert report_2.pending == ["0004_add_monthly_recurrence"]
        assert report_2.applied == ["0004_add_monthly_recurrence"]

        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute(
                "select tag from __drizzle_migrations order by applied_at"
            )
            tracked = [r[0] for r in cur.fetchall()]
            assert tracked == [
                "0000_baseline",
                "0001_agent_telemetry",
                "0002_recurring_supply",
                "0003_recurring_need_drafts",
                "0004_add_monthly_recurrence",
            ]
        finally:
            conn.close()
    finally:
        drop()


async def test_second_run_after_full_migration_is_a_true_no_op():
    """Idempotence (mandat §4/§6) : rejouer le runner sur une DB déjà à jour
    ne touche rien et ne lève rien."""
    import db_tools

    from ladini.schema_migrations.runner import apply_pending_migrations

    admin = _require_admin_dsn()
    dsn, drop = db_tools.create_database(admin)
    try:
        first = await apply_pending_migrations(dsn)
        assert first.applied  # toutes les migrations, base neuve

        second = await apply_pending_migrations(dsn)
        assert second.pending == []
        assert second.applied == []
        assert second.current_before == "0004_add_monthly_recurrence"
    finally:
        drop()


async def test_a_broken_migration_stops_the_run_and_leaves_no_trace(tmp_path):
    """Migration cassée -> `MigrationFailure`, rien n'est enregistré pour
    elle (le prochain run la retenterait, jamais un rejeu silencieux d'une
    migration `réussie`), et — point central du mandat §6/§7 — le process
    appelant (`scripts/*.sh`, via le CLI) doit voir un échec net, jamais un
    "migrations appliquées" mensonger."""
    import db_tools

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

        import psycopg2

        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("select tag from __drizzle_migrations")
            tracked = [r[0] for r in cur.fetchall()]
            # 0000_ok est bien passée (sa propre transaction a commité) ;
            # 0001_broken n'a JAMAIS été marquée appliquée.
            assert tracked == ["0000_ok"]

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
