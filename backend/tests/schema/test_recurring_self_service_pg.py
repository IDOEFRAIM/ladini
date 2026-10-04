"""B21 — RECURRING SELF-SERVICE BUYER contre un VRAI PostgreSQL : sans cron, sans digest.

Parcours : besoin ACTIF sans occurrence -> `ensure_next_recurring_occurrence` (même primitive que le cron) ->
`refresh_recurring_need_matching` (même moteur que le cron) -> détail -> `accept_match_proposal` en MODE EXACT
(`occurrence_id` + `expected_version`, jamais de digest simulé) -> commandes. `notified_at` reste NULL partout.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import psycopg2
from factories import Graph
from psycopg2.extras import Json
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_recurring_e2e_pg import _identities, _run, _sql, _Svc

from ladini.workers.automation.need_matching_service import NeedMatchingService
from ladini.workers.automation.recurring_supply_digest_service import (
    RecurringSupplyDigestService,
)


def _world(dsn, *, requested=350, stock=250, products=()):
    """Un besoin ACTIF journalier SANS aucune occurrence, et les produits proposés (liste de (stock, prix))."""
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=requested, unit="KG", recurrence_type="DAILY",
                                starts_at=datetime.utcnow() - timedelta(days=1))
        prods = []
        for i, (qty, price) in enumerate(products or ([(stock, 500)] if stock else [])):
            producer = g.producer if i == 0 else g.extra_producer()
            prods.append(g.product_for(producer=producer, quantity_for_sale=qty, price=price))
    conn.close()
    return g, need, prods


def _call(dsn, g, name, *args, **kw):
    phone, buyer, producer = _identities(dsn, g)

    async def go(session):
        return await getattr(_Svc(session, buyer, producer), name)(phone, *args, **kw)

    return _run(dsn, go)


def _occ(dsn, need):
    rows = _sql(dsn, "select id, status, version, quantity_matched, quantity_confirmed, notified_at, occurrence_date "
                     "from marketplace.recurring_need_occurrences where recurring_need_id=%s order by occurrence_date", (str(need),))
    return rows


def _first_occ(dsn, need):
    return _occ(dsn, need)[0]


def _orders(dsn, g):
    return _sql(dsn, "select o.id, sum(oi.quantity) from marketplace.orders o join marketplace.order_items oi on oi.order_id=o.id "
                     "where o.buyer_id=%s and o.order_type='RECURRING_SUPPLY' group by o.id", (str(g.buyer),))


# ── 1. le parcours complet SANS cron ET SANS digest ─────────────────────────────────────────────────────────
def test_full_self_service_cycle_without_cron_or_digest_partial_accept(pg_dsn):
    g, need, (prod,) = _world(pg_dsn, requested=350, stock=250)
    assert _occ(pg_dsn, need) == []  # aucune occurrence, aucun cron, aucun digest

    ensured = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))
    assert ensured["occurrence_id"] and ensured["created"] >= 1 and ensured["occurrence_status"] == "OPEN"
    again = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))  # idempotent
    assert again["occurrence_id"] == ensured["occurrence_id"] and again["created"] == 0

    detail = _call(pg_dsn, g, "get_recurring_need_detail", str(need))
    assert detail["occurrence_status"] == "OPEN" and detail["allocations"] == []

    res = _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))
    assert res["outcome"] == "MATCHED" and res["quantity_matched"] == 250.0 and res["changed"] is True
    v = res["occurrence_version"]
    detail = _call(pg_dsn, g, "get_recurring_need_detail", str(need))
    assert detail["occurrence_version"] == v and [a["quantity"] for a in detail["allocations"]] == [250.0]

    acc = _call(pg_dsn, g, "accept_match_proposal", str(need), "ACCEPT", occurrence_id=detail["occurrence_id"],
                expected_version=v)
    assert acc["status"] == "success" and acc.get("quantity_confirmed") == 250.0 and len(acc["order_ids"]) == 1
    occ = _first_occ(pg_dsn, need)
    assert occ[1] == "PARTIALLY_ACCEPTED" and occ[5] is None  # notified_at JAMAIS posé : aucun digest n'a existé
    assert [float(q) for _o, q in _orders(pg_dsn, g)] == [250.0]
    assert float(_sql(pg_dsn, "select quantity_for_sale from marketplace.products where id=%s", (str(prod),))[0][0]) == 0.0

    after = _call(pg_dsn, g, "get_recurring_need_detail", str(need))  # « mes besoins » reste valide après acceptation
    assert after["occurrence_status"] == "PARTIALLY_ACCEPTED" and [o["quantity"] for o in after["orders"]] == [250.0]
    lst = _call(pg_dsn, g, "list_my_recurring_needs")
    assert lst["items"][0]["next_occurrence_status"] == "PARTIALLY_ACCEPTED"
    # double acceptation : aucune commande en plus
    dup = _call(pg_dsn, g, "accept_match_proposal", str(need), "ACCEPT", occurrence_id=detail["occurrence_id"], expected_version=v)
    assert dup["outcome"] == "ALREADY_PROCESSED" and len(_orders(pg_dsn, g)) == 1


def test_complete_availability_is_accepted_and_orders_350(pg_dsn):
    g, need, _ = _world(pg_dsn, requested=350, products=[(400, 500)])
    res = _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))  # ensure + match en un appel
    assert res["outcome"] == "MATCHED"
    occ = _first_occ(pg_dsn, need)
    assert occ[1] == "MATCHED"
    acc = _call(pg_dsn, g, "accept_match_proposal", str(need), "ACCEPT", occurrence_id=str(occ[0]), expected_version=occ[2])
    assert acc["status"] == "success"
    assert _first_occ(pg_dsn, need)[1] == "ACCEPTED" and sum(float(q) for _o, q in _orders(pg_dsn, g)) == 350.0


def test_zero_availability_no_accept_possible_and_retry_works(pg_dsn):
    g, need, _ = _world(pg_dsn, requested=350, stock=0)
    res = _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))
    assert res["outcome"] == "NO_AVAILABILITY" and res["quantity_matched"] == 0.0
    occ = _first_occ(pg_dsn, need)
    refused = _call(pg_dsn, g, "accept_match_proposal", str(need), "ACCEPT", occurrence_id=str(occ[0]), expected_version=occ[2])
    assert refused["outcome"] == "NO_PROPOSAL" and _orders(pg_dsn, g) == []
    # un produit apparaît : « rechercher à nouveau » aboutit
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        g.product_for(quantity_for_sale=500, price=450)
    conn.close()
    assert _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))["outcome"] == "MATCHED"


def test_version_change_and_stock_change_refuse_without_any_order(pg_dsn):
    g, need, (prod,) = _world(pg_dsn, requested=350, stock=250)
    v1 = _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))["occurrence_version"]
    occ_id = str(_first_occ(pg_dsn, need)[0])
    # le matching change (stock 250 -> 300) => version V+1 ; l'ancien menu (V) est refusé
    _sql(pg_dsn, "update marketplace.products set quantity_for_sale=300 where id=%s", (str(prod),))
    v2 = _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))["occurrence_version"]
    assert v2 == v1 + 1
    stale = _call(pg_dsn, g, "accept_match_proposal", str(need), "ACCEPT", occurrence_id=occ_id, expected_version=v1)
    assert stale["outcome"] == "PROPOSAL_CHANGED" and _orders(pg_dsn, g) == []
    # le stock fond APRÈS l'affichage (sans rematch) : STOCK_CHANGED, aucune commande incohérente
    _sql(pg_dsn, "update marketplace.products set quantity_for_sale=100 where id=%s", (str(prod),))
    short = _call(pg_dsn, g, "accept_match_proposal", str(need), "ACCEPT", occurrence_id=occ_id, expected_version=v2)
    assert short["outcome"] == "STOCK_CHANGED" and _orders(pg_dsn, g) == []


def test_reject_self_service_uses_the_existing_service(pg_dsn):
    g, need, _ = _world(pg_dsn, requested=100, stock=500)
    v = _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))["occurrence_version"]
    occ = _first_occ(pg_dsn, need)
    rej = _call(pg_dsn, g, "accept_match_proposal", str(need), "REJECT", occurrence_id=str(occ[0]), expected_version=v)
    assert rej["action"] == "REJECT" and _first_occ(pg_dsn, need)[1] == "REJECTED" and _orders(pg_dsn, g) == []


def test_multi_producer_acceptance_creates_one_order_per_producer(pg_dsn):
    g, need, (a, b) = _world(pg_dsn, requested=350, products=[(200, 500), (150, 520)])
    ensured = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))
    occ_id = ensured["occurrence_id"]
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g.cur = cur
        for prod, qty, price in ((a, 200, 500), (b, 150, 520)):
            producer = _sql(pg_dsn, "select producer_id from marketplace.products where id=%s", (str(prod),))[0][0]
            g.allocation(occurrence=occ_id, producer=producer, product=prod, quantity=qty, unit_price=price, unit="KG")
        cur.execute("update marketplace.recurring_need_occurrences set quantity_matched=350, status='MATCHED', version=version+1 "
                    "where id=%s", (occ_id,))
    conn.close()
    occ = _first_occ(pg_dsn, need)
    acc = _call(pg_dsn, g, "accept_match_proposal", str(need), "ACCEPT", occurrence_id=occ_id, expected_version=occ[2])
    assert len(acc["order_ids"]) == 2 and sorted(float(q) for _o, q in _orders(pg_dsn, g)) == [150.0, 200.0]
    detail = _call(pg_dsn, g, "get_recurring_need_detail", str(need))
    assert detail["occurrence_status"] == "ACCEPTED" and sorted(o["quantity"] for o in detail["orders"]) == [150.0, 200.0]


# ── 2. concurrence, convergence cron / self-service / digest ─────────────────────────────────────────────────
def test_concurrent_cron_and_buyer_materialization_create_exactly_one_occurrence_per_date(pg_dsn):
    g, need, _ = _world(pg_dsn, requested=100, stock=100)
    phone, buyer, producer = _identities(pg_dsn, g)

    async def go():
        engine = create_async_engine(pg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async def cron():
                async with AsyncSession(engine, expire_on_commit=False) as s:
                    await _Svc(s, buyer, producer).replenish_occurrence_windows()
                    await s.commit()

            async def buyer_call():
                async with AsyncSession(engine, expire_on_commit=False) as s:
                    r = await _Svc(s, buyer, producer).ensure_next_recurring_occurrence(phone, str(need))
                    await s.commit()
                    return r

            return await asyncio.gather(cron(), buyer_call(), buyer_call(), cron(), return_exceptions=True)
        finally:
            await engine.dispose()

    results = asyncio.run(go())
    assert not [r for r in results if isinstance(r, Exception)], results
    dates = [r[6] for r in _occ(pg_dsn, need)]
    assert dates and len(dates) == len(set(dates))  # aucune occurrence en double
    assert len(dates) == 8  # fenêtre d'aujourd'hui à +7 jours


def test_cron_matching_then_self_service_is_identical_and_does_not_bump_the_version(pg_dsn):
    g, need, _ = _world(pg_dsn, requested=350, stock=250)
    ensured = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))

    async def cron_match(session):
        return await NeedMatchingService(session).rematch_occurrence(ensured["occurrence_id"], trigger="scheduled")

    report = _run(pg_dsn, cron_match)
    assert report.changed is True
    before = _first_occ(pg_dsn, need)
    assert before[5] is None  # matché par le cron, digest PAS encore envoyé
    detail = _call(pg_dsn, g, "get_recurring_need_detail", str(need))  # visible immédiatement, sans notified_at
    assert detail["occurrence_version"] == before[2] and [a["quantity"] for a in detail["allocations"]] == [250.0]
    res = _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))  # proposition identique
    assert res["changed"] is False and res["occurrence_version"] == before[2]
    n_alloc = _sql(pg_dsn, "select count(*) from marketplace.need_allocations where occurrence_id=%s and status='PROPOSED'",
                   (ensured["occurrence_id"],))[0][0]
    assert n_alloc == 1


def test_digest_after_self_service_accept_is_not_actionable_and_self_service_after_digest_converges(pg_dsn):
    g, need, _ = _world(pg_dsn, requested=100, stock=500)
    res = _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))
    occ = _first_occ(pg_dsn, need)
    target = occ[6].date()

    async def digest(session):
        await RecurringSupplyDigestService(session).run(target_date=target)

    # digest AVANT l'acceptation : même occurrence / même version que le self-service
    _run(pg_dsn, digest)
    after_digest = _first_occ(pg_dsn, need)
    detail = _call(pg_dsn, g, "get_recurring_need_detail", str(need))
    assert detail["occurrence_id"] == str(after_digest[0]) and detail["occurrence_version"] == res["occurrence_version"]
    assert len(_occ(pg_dsn, need)) == 8  # le digest/self-service n'a créé aucune occurrence en plus
    # acceptation self-service, puis le digest rejoue : plus aucune proposition actionable pour cette occurrence
    acc = _call(pg_dsn, g, "accept_match_proposal", str(need), "ACCEPT", occurrence_id=detail["occurrence_id"],
                expected_version=detail["occurrence_version"])
    assert acc["status"] == "success" and len(_orders(pg_dsn, g)) == 1
    outbox_before = _sql(pg_dsn, "select count(*) from intelligence.notification_outbox where payload::text like %s",
                         (f"%{detail['occurrence_id']}%",))[0][0]
    _run(pg_dsn, digest)
    outbox_after = _sql(pg_dsn, "select count(*) from intelligence.notification_outbox where payload::text like %s",
                        (f"%{detail['occurrence_id']}%",))[0][0]
    assert outbox_after == outbox_before and len(_orders(pg_dsn, g)) == 1


def test_terminal_occurrence_shows_requested_vs_delivered_and_no_accept(pg_dsn):
    g, need, _ = _world(pg_dsn, requested=350, stock=250)
    _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))
    _sql(pg_dsn, "update marketplace.recurring_need_occurrences set status='PARTIALLY_FULFILLED', quantity_delivered=200 "
                 "where recurring_need_id=%s and occurrence_date=(select min(occurrence_date) from "
                 "marketplace.recurring_need_occurrences where recurring_need_id=%s)", (str(need), str(need)))
    detail = _call(pg_dsn, g, "get_recurring_need_detail", str(need))
    # la prochaine occurrence pertinente est la suivante (OPEN) ; la terminale n'est pas ré-affichée comme actionnable
    assert detail["occurrence_status"] == "OPEN"
    only = _sql(pg_dsn, "select status, quantity_delivered from marketplace.recurring_need_occurrences where recurring_need_id=%s "
                        "order by occurrence_date limit 1", (str(need),))[0]
    assert only[0] == "PARTIALLY_FULFILLED" and float(only[1]) == 200.0


def test_package_products_are_never_matched_by_the_self_service(pg_dsn):
    g, need, (prod,) = _world(pg_dsn, requested=100, stock=500)
    _sql(pg_dsn, "update marketplace.products set pricing_tiers=%s where id=%s", (Json([
        {"tier_id": "t1", "quantity": 0.5, "unit": "KG", "price": 500, "packaging": "sac", "base_unit_quantity": 0.5,
         "min_order_quantity": 1, "available_count": 1000}]), str(prod)))
    assert _call(pg_dsn, g, "refresh_recurring_need_matching", str(need))["outcome"] == "NO_AVAILABILITY"
    assert _orders(pg_dsn, g) == []


def test_concurrent_manual_and_cron_matching_converge_without_duplicate_allocations(pg_dsn):
    g, need, _ = _world(pg_dsn, requested=350, stock=250)
    occ_id = _call(pg_dsn, g, "ensure_next_recurring_occurrence", str(need))["occurrence_id"]
    phone, buyer, producer = _identities(pg_dsn, g)

    async def go():
        engine = create_async_engine(pg_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async def cron():
                async with AsyncSession(engine, expire_on_commit=False) as s:
                    return (await NeedMatchingService(s).rematch_occurrence(occ_id, trigger="scheduled")).changed

            async def manual():
                async with AsyncSession(engine, expire_on_commit=False) as s:
                    r = await _Svc(s, buyer, producer).refresh_recurring_need_matching(phone, str(need))
                    await s.commit()
                    return r

            return await asyncio.gather(cron(), manual(), cron(), manual(), return_exceptions=True)
        finally:
            await engine.dispose()

    results = asyncio.run(go())
    assert not [r for r in results if isinstance(r, Exception)], results
    n_alloc = _sql(pg_dsn, "select count(*) from marketplace.need_allocations where occurrence_id=%s and status='PROPOSED'", (occ_id,))[0][0]
    assert n_alloc == 1
    occ = _first_occ(pg_dsn, need)
    assert occ[2] == 2 and float(occ[3]) == 250.0  # UN seul bump de version (la proposition ne change qu'une fois)
