"""Recurring ADMIN — réglage du délai avant première livraison + console OPERATIONS, contre un vrai PostgreSQL.

Couvre : défaut (4 jours), lecture/écriture admin, autorisation, validation, audit, « un changement ne touche
que les besoins créés ensuite », date explicite valide / trop proche, alignement WEEKLY/MONTHLY, et la vue
opérationnelle (liste filtrable + fiche d'un besoin : occurrences, allocations, commandes).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

import psycopg2
import pytest
import test_recurring_supply_service as base
from factories import insert, uniq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import ladini.services.recurring_admin.api as api
from ladini.domain.models import RecurringNeed

# Fixtures/helpers du service de besoins récurrents, réexposés sans redéfinition (même idiome que les tests B22).
market = base.market
_fixed_today = base._fixed_today
_run = base._run
_Svc = base._Svc
FIXED_TODAY = base.FIXED_TODAY


@pytest.fixture(autouse=True)
def _api_today(monkeypatch):
    monkeypatch.setattr(api, "_today", lambda: FIXED_TODAY)


@pytest.fixture
def admin(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        uid = insert(cur, "auth.users", phone=uniq("+226"), name="Admin", role="ADMIN")
    conn.close()
    return str(uid)


@pytest.fixture
def buyer_user(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        uid = insert(cur, "auth.users", phone=uniq("+226"), name="Awa", role="BUYER")
    conn.close()
    return str(uid)


def _svc(market_tuple, session):
    _dsn, user, profile = market_tuple
    svc = _Svc(session, user)
    svc._user_profile = profile
    return svc


def _create(market_tuple, **over):
    cols = dict(phone="+226", product_query="tomate", quantity=40, unit="KG", recurrence_type="DAILY")
    cols.update(over)

    async def fn(session):
        return await _svc(market_tuple, session).create_recurring_need(**cols)

    return _run(market_tuple[0], fn)


def _set_lead(dsn, admin_id, days):
    return _run(dsn, lambda s: api.write_settings(s, actor_id=admin_id, minimum_start_lead_days=days))


# ─── configuration ────────────────────────────────────────────────────


def test_default_lead_is_four_days_from_the_single_source(pg_dsn):
    out = _run(pg_dsn, api.read_settings)["recurring"]
    assert out["minimum_start_lead_days"] == 4 and out["source"] == "DEFAULT" and out["version"] == 0


def test_admin_can_update_and_the_change_is_audited(pg_dsn, admin):
    out = _set_lead(pg_dsn, admin, 7)["recurring"]
    assert out["minimum_start_lead_days"] == 7 and out["source"] == "DATABASE" and out["version"] == 1
    assert _run(pg_dsn, api.read_settings)["recurring"]["minimum_start_lead_days"] == 7

    conn = psycopg2.connect(pg_dsn)
    with conn.cursor() as cur:
        cur.execute(
            "select old_value, new_value from intelligence.audit_logs where action = %s and actor_id = %s",
            ("PLATFORM_SETTING_CHANGED", admin),
        )
        rows = cur.fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0]["minimum_start_lead_days"] == 4 and rows[0][1]["minimum_start_lead_days"] == 7


def test_a_non_admin_cannot_update(pg_dsn, buyer_user):
    with pytest.raises(api.ApiError) as err:
        _set_lead(pg_dsn, buyer_user, 9)
    assert err.value.status == 403
    assert _run(pg_dsn, api.read_settings)["recurring"]["source"] == "DEFAULT"


@pytest.mark.parametrize("bad", [-1, 31, 2.5, "abc", None, True])
def test_invalid_values_are_rejected_and_nothing_is_written(pg_dsn, admin, bad):
    with pytest.raises(api.ApiError) as err:
        _set_lead(pg_dsn, admin, bad)
    assert err.value.status == 422
    assert _run(pg_dsn, api.read_settings)["recurring"]["source"] == "DEFAULT"


def test_unknown_or_missing_actor_is_refused(pg_dsn):
    with pytest.raises(api.ApiError) as err:
        _set_lead(pg_dsn, None, 5)
    assert err.value.status == 400
    with pytest.raises(api.ApiError) as err:
        _set_lead(pg_dsn, str(uuid.uuid4()), 5)
    assert err.value.status == 403


def test_a_stale_expected_version_is_a_conflict(pg_dsn, admin):
    _set_lead(pg_dsn, admin, 5)
    with pytest.raises(api.ApiError) as err:
        _run(pg_dsn, lambda s: api.write_settings(s, actor_id=admin, minimum_start_lead_days=6, expected_version=0))
    assert err.value.status == 409


# ─── dates de début ───────────────────────────────────────────────────


def test_no_explicit_date_starts_after_the_lead_time_never_tomorrow(market):
    result = _create(market)
    assert result["starts_at"] == (FIXED_TODAY + timedelta(days=4)).isoformat()
    assert result["start_adjusted"] is False
    assert result["first_delivery_date"] == (FIXED_TODAY + timedelta(days=4)).isoformat()


def test_a_too_early_explicit_date_is_pushed_back_and_reported(market):
    result = _create(market, starts_at=(FIXED_TODAY + timedelta(days=1)).isoformat())
    assert result["start_adjusted"] is True
    assert result["requested_start"] == (FIXED_TODAY + timedelta(days=1)).isoformat()
    assert result["starts_at"] == (FIXED_TODAY + timedelta(days=4)).isoformat()


def test_a_valid_explicit_date_is_respected(market):
    result = _create(market, starts_at="2026-10-20")
    assert result["starts_at"] == "2026-10-20" and result["start_adjusted"] is False


def test_the_setting_applies_to_new_needs_only(market, admin):
    dsn = market[0]
    need_a = _create(market, product_query="tomate")
    _set_lead(dsn, admin, 7)
    need_b = _create(market, product_query="tomate", unit="KG", quantity=41)

    async def starts(session):
        out = {}
        for key, res in (("a", need_a), ("b", need_b)):
            need = await session.get(RecurringNeed, uuid.UUID(res["recurring_need_id"]))
            out[key] = need.starts_at.date()
        return out

    got = _run(dsn, starts)
    assert got["a"] == FIXED_TODAY + timedelta(days=4)  # inchangé après le passage 4 -> 7
    assert got["b"] == FIXED_TODAY + timedelta(days=7)


def test_lead_zero_allows_today(market, admin):
    _set_lead(market[0], admin, 0)
    assert _create(market)["starts_at"] == FIXED_TODAY.isoformat()


def test_weekly_and_monthly_first_dates_respect_the_minimum(market):
    weekly = _create(market, product_query="tomate", recurrence_type="WEEKLY", quantity=1)
    minimum = FIXED_TODAY + timedelta(days=4)
    assert weekly["first_delivery_date"] == minimum.isoformat()
    monthly = _create(market, product_query="tomate", recurrence_type="MONTHLY", quantity=2)
    assert monthly["first_delivery_date"] == minimum.isoformat()  # ancre = le jour retenu, jamais avant le minimum


def test_the_start_policy_tool_matches_the_creation_decision(market):
    async def fn(session):
        return await _svc(market, session).get_recurring_start_policy(starts_at=None)

    policy = _run(market[0], fn)
    assert policy["effective_start"] == (FIXED_TODAY + timedelta(days=4)).isoformat() and policy["lead_days"] == 4


# ─── console OPERATIONS (lecture) ─────────────────────────────────────


def _counts(dsn):
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("select (select count(*) from marketplace.recurring_needs), (select count(*) from marketplace.recurring_need_occurrences)")
        out = cur.fetchone()
    conn.close()
    return out


def test_list_shows_the_operational_columns_and_filters(market):
    dsn = market[0]
    created = _create(market)
    before = _counts(dsn)

    page = _run(dsn, lambda s: api.list_needs(s, api.ListParams()))
    row = next(i for i in page["items"] if i["id"] == created["recurring_need_id"])
    for key in ("short_id", "buyer", "product", "quantity", "unit", "frequency", "status", "starts_at", "created_at",
                "next_occurrence", "region", "occurrence_count", "sourcing_state", "last_activity"):
        assert key in row
    assert row["product"] == "tomate" and row["frequency"] == "DAILY" and row["occurrence_count"] == 4
    assert row["next_occurrence"].startswith((FIXED_TODAY + timedelta(days=4)).isoformat())

    def ids(params):
        return {i["id"] for i in _run(dsn, lambda s: api.list_needs(s, params))["items"]}

    assert created["recurring_need_id"] in ids(api.ListParams(status="ACTIVE", frequency="DAILY", product="tom"))
    assert created["recurring_need_id"] not in ids(api.ListParams(status="CANCELLED"))
    assert created["recurring_need_id"] not in ids(api.ListParams(frequency="MONTHLY"))
    assert created["recurring_need_id"] in ids(api.ListParams(q=created["recurring_need_id"][:8]))
    assert created["recurring_need_id"] in ids(api.ListParams(starts_from=date(2026, 9, 19), starts_to=date(2026, 9, 19)))
    assert _counts(dsn) == before, "la console ne modifie jamais rien"


def test_need_detail_exposes_schedule_occurrences_and_an_honest_mutation_gap(market):
    dsn = market[0]
    created = _create(market)
    detail = _run(dsn, lambda s: api.get_need_detail(s, created["recurring_need_id"]))
    assert set(detail) >= {"overview", "schedule", "occurrences", "orders", "mutations", "operational_state"}
    assert detail["overview"]["starts_at"].startswith("2026-09-19")
    assert detail["schedule"]["first_delivery_date"] == "2026-09-19"
    assert detail["schedule"]["lead_time_policy"]["current_minimum_start_lead_days"] == 4
    assert detail["schedule"]["lead_time_policy"]["applied_at_creation"] is None  # jamais inventé
    assert [o["date"][:10] for o in detail["occurrences"]] == ["2026-09-19", "2026-09-20", "2026-09-21", "2026-09-22"]
    assert detail["mutations"]["available"] is False  # pas de faux historique


def test_unknown_need_is_a_404(pg_dsn):
    for raw in ("not-a-uuid", str(uuid.uuid4())):
        with pytest.raises(api.ApiError) as err:
            _run(pg_dsn, lambda s, raw=raw: api.get_need_detail(s, raw))
        assert err.value.status == 404


def test_the_settings_table_exists_with_its_audit_dependencies(pg_dsn):
    async def go():
        engine = create_async_engine(pg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine) as session:
                return (await session.execute(text("select count(*) from governance.platform_settings"))).scalar()
        finally:
            await engine.dispose()

    assert asyncio.run(go()) >= 0
