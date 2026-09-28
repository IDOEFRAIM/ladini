"""Market Balance (Phase E) — real-Postgres tests for `recompute_today`: the mission's own ETAPE 39
checklist (A-I). `recurring_need_occurrences`/`auctions`/`producer_supply_daily_snapshot` are
queried GLOBALLY by `recompute_today` (no test-scoping in the SQL itself — same as
`snapshot_producer_supply`), so every assertion here reads back only THIS test's own rows, filtered
by its own unique `sub_category_id`/`zone_scope` (fresh per `Graph` instance) — the same isolation
strategy `test_producer_metric_layer_pg.py`'s own supply-snapshot tests already use, for the same
reason: `snapshot_day`/`recompute_today` always operate on real "today", shared by the whole
session, so day-based isolation does not apply here.

NOTE: `Graph.cur` is only valid inside the `with conn, conn.cursor() as cur: g.cur = cur` block
that creates it (the `world` fixture's own connection is already closed by the time a test runs) —
every write via a `Graph` method happens inside such a block in this file, never after it closes."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.services.analytics.market_balance_refresh import recompute_today
from ladini.services.analytics.producer_metrics_refresh import snapshot_producer_supply


def _run(dsn, fn):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _sql(dsn, statement, params=()):
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(statement, params)
        out = cur.fetchall() if cur.description else None
    conn.close()
    return out


@pytest.fixture
def world(pg_dsn):
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        # Graph's own buyer user has no zone by default — Market Balance needs one (RECURRING
        # demand's zone is the buyer's own `auth.users.zone_id`, docs/analytics/MARKET_BALANCE.md §10).
        cur.execute("update auth.users set zone_id = %s where id = %s", (g.zone, g.buyer_user))
    conn.close()
    return pg_dsn, g


def _rows_for(dsn, g, *, unit=None):
    q = ("select canonical_unit, open_demand_quantity, available_supply_quantity, potential_coverable_quantity, "
         "demand_gap_quantity, excess_supply_quantity, demand_scope, computed_at "
         "from analytics.market_balance_daily_snapshot where sub_category_id = %s and zone_scope = %s")
    params = [str(g.sub_category), str(g.zone)]
    if unit:
        q += " and canonical_unit = %s"
        params.append(unit)
    return _sql(dsn, q, tuple(params))


def _float(row_col):
    return float(row_col)


class TestCoreFormulas:
    def test_a_demand_exceeds_supply_yields_the_exact_gap(self, world):
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            need = g.recurring_need(sub_category_id=g.sub_category, unit="KG")
            g.occurrence(need=need, requested_quantity=1000, unit="KG", status="OPEN", quantity_confirmed=0)
            g.product_for(quantity_for_sale=800, is_available=True, unit="KG")
        conn.close()
        today = date.today()
        _run(dsn, lambda s: snapshot_producer_supply(s, today))
        _run(dsn, recompute_today)

        rows = _rows_for(dsn, g, unit="KG")
        assert len(rows) == 1
        _, open_d, supply, coverable, gap, excess, scope, _ = rows[0]
        assert (_float(open_d), _float(supply), _float(coverable), _float(gap), _float(excess)) == (1000.0, 800.0, 800.0, 200.0, 0.0)
        assert scope == "RECURRING"

    def test_b_supply_exceeds_demand_yields_the_exact_excess(self, world):
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            need = g.recurring_need(sub_category_id=g.sub_category, unit="KG")
            g.occurrence(need=need, requested_quantity=500, unit="KG", status="OPEN", quantity_confirmed=0)
            g.product_for(quantity_for_sale=900, is_available=True, unit="KG")
        conn.close()
        today = date.today()
        _run(dsn, lambda s: snapshot_producer_supply(s, today))
        _run(dsn, recompute_today)

        rows = _rows_for(dsn, g, unit="KG")
        _, open_d, supply, coverable, gap, excess, _, _ = rows[0]
        assert (_float(open_d), _float(supply), _float(coverable), _float(gap), _float(excess)) == (500.0, 900.0, 500.0, 0.0, 400.0)


class TestUnits:
    def test_c_kg_and_tete_never_combine_into_one_row(self, world):
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            need = g.recurring_need(sub_category_id=g.sub_category, unit="KG")
            g.occurrence(need=need, requested_quantity=100, unit="KG", status="OPEN", quantity_confirmed=0)
            need2 = g.recurring_need(sub_category_id=g.sub_category, unit="TETE", quantity=1)
            g.occurrence(need=need2, requested_quantity=12, unit="TETE", status="MATCHED", quantity_confirmed=0)
        conn.close()
        _run(dsn, recompute_today)

        rows = {r[0]: r for r in _rows_for(dsn, g)}
        assert set(rows) == {"KG", "TETE"}
        assert _float(rows["KG"][1]) == 100.0
        assert _float(rows["TETE"][1]) == 12.0

    def test_d_grams_convert_to_kg_via_the_subcategorys_priority_unit(self, world):
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            cur.execute("update governance.sub_categories set priority_unit = %s where id = %s", ("KG", str(g.sub_category)))
            need = g.recurring_need(sub_category_id=g.sub_category, unit="G")
            g.occurrence(need=need, requested_quantity=500, unit="G", status="OPEN", quantity_confirmed=0)
        conn.close()
        _run(dsn, recompute_today)

        rows = _rows_for(dsn, g, unit="KG")
        assert len(rows) == 1
        assert _float(rows[0][1]) == pytest.approx(0.5)  # 500 G -> 0.5 KG


class TestIsolation:
    def test_e_two_zones_never_share_a_cell(self, world):
        dsn, g1 = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g2 = Graph(cur)
            cur.execute("update auth.users set zone_id = %s where id = %s", (g2.zone, g2.buyer_user))
            g1.cur = cur
            need1 = g1.recurring_need(sub_category_id=g1.sub_category, unit="KG")
            g1.occurrence(need=need1, requested_quantity=300, unit="KG", status="OPEN", quantity_confirmed=0)
            g2.cur = cur
            need2 = g2.recurring_need(sub_category_id=g2.sub_category, unit="KG")
            g2.occurrence(need=need2, requested_quantity=700, unit="KG", status="OPEN", quantity_confirmed=0)
        conn.close()
        _run(dsn, recompute_today)

        rows1 = _rows_for(dsn, g1, unit="KG")
        rows2 = _rows_for(dsn, g2, unit="KG")
        assert _float(rows1[0][1]) == 300.0
        assert _float(rows2[0][1]) == 700.0

    def test_f_two_subcategories_never_share_a_cell(self, world):
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            other_cat = insert(cur, "governance.categories", name=uniq("cat2"))
            other_sub = insert(cur, "governance.sub_categories", category_id=other_cat, name=uniq("sub2"))
            need1 = g.recurring_need(sub_category_id=g.sub_category, unit="KG")
            g.occurrence(need=need1, requested_quantity=40, unit="KG", status="OPEN", quantity_confirmed=0)
            need2 = g.recurring_need(sub_category_id=other_sub, unit="KG")
            g.occurrence(need=need2, requested_quantity=90, unit="KG", status="OPEN", quantity_confirmed=0)
        conn.close()
        _run(dsn, recompute_today)

        rows_own = _rows_for(dsn, g, unit="KG")
        rows_other = _sql(
            dsn, "select open_demand_quantity from analytics.market_balance_daily_snapshot where sub_category_id = %s and zone_scope = %s and canonical_unit = 'KG'",
            (str(other_sub), str(g.zone)),
        )
        assert _float(rows_own[0][1]) == 40.0
        assert _float(rows_other[0][0]) == 90.0


class TestRecomputeIdempotency:
    def test_g_replaying_the_same_day_yields_identical_business_columns(self, world):
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            need = g.recurring_need(sub_category_id=g.sub_category, unit="KG")
            g.occurrence(need=need, requested_quantity=250, unit="KG", status="OPEN", quantity_confirmed=0)
            g.product_for(quantity_for_sale=100, is_available=True, unit="KG")
        conn.close()
        today = date.today()
        _run(dsn, lambda s: snapshot_producer_supply(s, today))

        _run(dsn, recompute_today)
        before = _sql(
            dsn, "select to_jsonb(t) - 'id' - 'computed_at' from analytics.market_balance_daily_snapshot t "
                 "where sub_category_id = %s and zone_scope = %s and canonical_unit = 'KG'", (str(g.sub_category), str(g.zone)),
        )
        _run(dsn, recompute_today)
        after = _sql(
            dsn, "select to_jsonb(t) - 'id' - 'computed_at' from analytics.market_balance_daily_snapshot t "
                 "where sub_category_id = %s and zone_scope = %s and canonical_unit = 'KG'", (str(g.sub_category), str(g.zone)),
        )
        assert before == after
        assert len(before) == 1

    def test_h_computed_at_is_fresh_right_after_a_recompute(self, world):
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            need = g.recurring_need(sub_category_id=g.sub_category, unit="KG")
            g.occurrence(need=need, requested_quantity=10, unit="KG", status="OPEN", quantity_confirmed=0)
        conn.close()
        _run(dsn, recompute_today)
        rows = _rows_for(dsn, g, unit="KG")
        computed_at = rows[0][7]
        assert (datetime.now(timezone.utc) - computed_at) < timedelta(minutes=2)


class TestNoFakeBackfill:
    def test_i_recompute_refuses_any_day_but_today(self, world):
        dsn, _g = world
        yesterday = date.today() - timedelta(days=1)
        with pytest.raises(ValueError, match="not reconstructible"):
            _run(dsn, lambda s: recompute_today(s, today=yesterday))


class TestActionableWindowAndStatus:
    def test_partially_accepted_occurrence_is_excluded_even_though_a_gap_remains(self, world):
        """docs/analytics/MARKET_BALANCE.md SS3.3: once an occurrence leaves OPEN/MATCHED it can
        never receive another allocation — a PARTIALLY_ACCEPTED occurrence's remaining gap is not
        'open demand', even though the raw subtraction is still positive."""
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            need = g.recurring_need(sub_category_id=g.sub_category, unit="KG")
            g.occurrence(need=need, requested_quantity=100, unit="KG", status="PARTIALLY_ACCEPTED", quantity_confirmed=40)
        conn.close()
        _run(dsn, recompute_today)
        assert _rows_for(dsn, g, unit="KG") == []

    def test_an_occurrence_outside_the_operational_window_is_excluded(self, world):
        dsn, g = world
        far_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=90)
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            need = g.recurring_need(sub_category_id=g.sub_category, unit="KG")
            g.occurrence(need=need, requested_quantity=100, unit="KG", status="OPEN", quantity_confirmed=0, occurrence_date=far_future)
        conn.close()
        _run(dsn, recompute_today)
        assert _rows_for(dsn, g, unit="KG") == []

    def test_an_expired_or_closed_auction_is_excluded_from_open_demand(self, world):
        dsn, g = world
        past_deadline = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            g.auction(sub_category_id=g.sub_category, target_zone_id=g.zone, quantity=250, unit="KG", status="OPEN", deadline=past_deadline)
            g.auction(sub_category_id=g.sub_category, target_zone_id=g.zone, quantity=999, unit="KG", status="CLOSED")
        conn.close()
        _run(dsn, recompute_today)
        assert _rows_for(dsn, g, unit="KG") == []

    def test_an_open_auction_within_deadline_is_included_as_tender_scope(self, world):
        dsn, g = world
        conn = psycopg2.connect(dsn)
        with conn, conn.cursor() as cur:
            g.cur = cur
            g.auction(sub_category_id=g.sub_category, target_zone_id=g.zone, quantity=250, unit="KG", status="OPEN")
        conn.close()
        _run(dsn, recompute_today)
        rows = _rows_for(dsn, g, unit="KG")
        assert len(rows) == 1
        assert _float(rows[0][1]) == 250.0
        assert rows[0][6] == "TENDER"
