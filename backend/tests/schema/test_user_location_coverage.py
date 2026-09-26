"""Migration 0005 (`auth.users.declared_location`/`coverage_status`) — mandat onboarding
2026-09-26 : preuve directe, sur PostgreSQL réel, que le défaut, le CHECK et le backfill
tiennent — au-delà de la comparaison de schéma statique (`test_schema_postgres.py`/
`test_schema_contract_static.py`), qui ne rejoue aucun DML."""
from __future__ import annotations

import asyncio

import pytest
from psycopg2 import errors
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


def _insert_user(cur, phone, **fields):
    columns = ["phone", *fields.keys()]
    placeholders = ["%s"] * len(columns)
    values = [phone, *fields.values()]
    cur.execute(
        f"INSERT INTO auth.users ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) RETURNING id",
        values,
    )
    return cur.fetchone()[0]


class TestCoverageStatusDefaultsAndConstraint:
    def test_a_row_with_no_explicit_coverage_status_defaults_to_covered(self, db):
        cur = db.cursor()
        user_id = _insert_user(cur, "+22670000101")
        cur.execute("select coverage_status, declared_location from auth.users where id = %s", (user_id,))
        row = cur.fetchone()
        assert row == ("COVERED", None)

    @pytest.mark.parametrize(
        "phone_suffix,status",
        [
            ("110", "COVERED"),
            ("111", "NEARBY"),
            ("112", "OUT_OF_COVERAGE"),
            ("113", "WAITLIST"),
        ],
    )
    def test_every_documented_coverage_status_is_accepted(self, db, phone_suffix, status):
        cur = db.cursor()
        user_id = _insert_user(cur, f"+226700001{phone_suffix}", coverage_status=status)
        cur.execute("select coverage_status from auth.users where id = %s", (user_id,))
        assert cur.fetchone() == (status,)

    def test_an_undocumented_coverage_status_is_rejected(self, db):
        cur = db.cursor()
        cur.execute("SAVEPOINT chk")
        with pytest.raises(errors.CheckViolation):
            _insert_user(cur, "+22670000199", coverage_status="MAYBE_LATER")
        cur.execute("ROLLBACK TO SAVEPOINT chk")

    def test_declared_location_is_free_text_independent_of_zone_id(self, db):
        """`declared_location` n'a AUCUNE contrainte de forme/référence — c'est précisément le
        point du mandat : la déclaration brute de l'utilisateur n'est jamais validée contre un
        référentiel fermé, contrairement à `zone_id` (FK)."""
        cur = db.cursor()
        user_id = _insert_user(
            cur, "+22670000102",
            declared_location="Un lieu-dit qui n'existe dans aucune table",
            coverage_status="OUT_OF_COVERAGE",
        )
        cur.execute("select declared_location, zone_id from auth.users where id = %s", (user_id,))
        assert cur.fetchone() == ("Un lieu-dit qui n'existe dans aucune table", None)


def _run(dsn, fn):
    """Même idiome que `test_need_matching_service.py` — une vraie `AsyncSession` asyncpg,
    jamais un mock, pour `BaseMixin` (qui n'attend RIEN d'autre que `self.session`)."""

    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _mixin(session):
    from ladini.services.database.base import BaseMixin

    class _Svc(BaseMixin):
        @property
        def session(self):
            return session

    return _Svc()


class TestCreateUserProfilePersistsLocationAndCoverage:
    """`agents/onboarding.py::_step_create_profile` -> `create_user_profile` -> `auth.users` :
    bout en bout sur PostgreSQL réel, pas seulement le contrat de `_FakeRuntime` (voir
    `tests/nodes/test_onboarding_confirmation_and_zone_coverage.py`, qui simule ce tool)."""

    def test_declared_location_and_coverage_status_reach_the_real_row(self, pg_dsn):
        async def go(session):
            svc = _mixin(session)
            result = await svc.create_user_profile(
                {
                    "phone": "+22670000210",
                    "name": "Gilbert-prod",
                    "role": "PRODUCER",
                    "zone_id": None,
                    "declared_location": "Somgande",
                    "coverage_status": "OUT_OF_COVERAGE",
                }
            )
            assert result["status"] == "success"
            await session.commit()

        _run(pg_dsn, go)

        async def check(session):
            from sqlalchemy import text

            row = (
                await session.execute(
                    text(
                        "select declared_location, coverage_status, zone_id "
                        "from auth.users where phone = :phone"
                    ),
                    {"phone": "+22670000210"},
                )
            ).one()
            assert row == ("Somgande", "OUT_OF_COVERAGE", None)

        _run(pg_dsn, check)

    def test_omitting_coverage_status_falls_back_to_the_column_default(self, pg_dsn):
        """`create_user_profile` ne force PAS `coverage_status` quand l'appelant ne le fournit
        pas (parité avec le comportement avant ce correctif) — le défaut DB ('COVERED')
        s'applique alors, jamais une valeur Python inventée."""

        async def go(session):
            svc = _mixin(session)
            result = await svc.create_user_profile(
                {"phone": "+22670000211", "name": "Awa", "role": "BUYER", "zone_id": None}
            )
            assert result["status"] == "success"
            await session.commit()

        _run(pg_dsn, go)

        async def check(session):
            from sqlalchemy import text

            row = (
                await session.execute(
                    text("select coverage_status from auth.users where phone = :phone"),
                    {"phone": "+22670000211"},
                )
            ).one()
            assert row == ("COVERED",)

        _run(pg_dsn, check)


class TestGetZoneHierarchyByNameOnRealPostgres:
    """`TestGetZoneHierarchyByNameWalksUpToARootZone` (test_onboarding_zone_region_level.py)
    couvre déjà la logique Python (session stubée) — ceci prouve la requête RÉELLE (opérateur
    trigram `%`, `pg_trgm`) sur des zones effectivement SEEDÉES, jamais commitées (la
    transaction n'est jamais validée, donc invisible aux autres tests de la session)."""

    def test_a_seeded_child_zone_resolves_to_its_seeded_parent_region(self, pg_dsn):
        async def go(session):
            from sqlalchemy import text

            region = (
                await session.execute(
                    text("insert into governance.climatic_regions (name) values (:n) returning id"),
                    {"n": "region-test-zone-hierarchy"},
                )
            ).scalar_one()
            root_id = (
                await session.execute(
                    text(
                        "insert into governance.zones (name, code, climatic_region_id) "
                        "values (:n, :c, :r) returning id"
                    ),
                    {"n": "Ouagadougou-Test-ZH", "c": "OUA-ZH", "r": region},
                )
            ).scalar_one()
            await session.execute(
                text(
                    "insert into governance.zones (name, code, climatic_region_id, parent_id) "
                    "values (:n, :c, :r, :p)"
                ),
                {"n": "Somgande-Test-ZH", "c": "SOM-ZH", "r": region, "p": root_id},
            )

            svc = _mixin(session)
            result = await svc.get_zone_hierarchy_by_name("Somgande-Test-ZH")
            assert result["status"] == "success"
            assert result["data"]["root"] == {"id": str(root_id), "name": "Ouagadougou-Test-ZH"}
            # Non-régression : `get_zone_by_name` (région uniquement) ne doit PAS voir cette
            # localité enfant — seule `get_zone_hierarchy_by_name` le peut.
            region_only = await svc.get_zone_by_name("Somgande-Test-ZH")
            assert region_only["status"] == "error"

        _run(pg_dsn, go)
