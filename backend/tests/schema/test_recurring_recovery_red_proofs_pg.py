"""B28 — PREUVES ROUGES : chaque test décrit le comportement ATTENDU et n'utilise que des API existantes avant B28
(sauf l'option `occurrence_id` quand elle existe). Rejoués sur la baseline (B27, sans B28) : R1/R2/R3/R8 échouent ; sur B28 : verts.
R4/R5/R6/R7 ne se reproduisent PAS sur la baseline (les garde-fous B12/B26 et l'index unique tiennent déjà) : ils restent comme
tests de non-régression, avec leur constat réel dans le rapport.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_recurring_fulfillment_pg import (
    _accept,
    _call,
    _confirm,
    _engine,
    _ids,
    _occ,
    _producer_cancel,
    _sql,
    _Svc,
)
from test_recurring_occurrence_recovery_pg import (
    TODAY,
    _allocs,
    _dt,
    _orders,
    _proposed,
    _world,
)

from ladini.services.database.recurring_supply import RecurringSupplyMixin
from ladini.workers.automation.need_matching_service import NeedMatchingService

_HAS_EXACT = "occurrence_id" in inspect.signature(RecurringSupplyMixin.refresh_recurring_need_matching).parameters


def _relaunch(dsn, w, occ):
    """« Cherche quelqu'un d'autre » : exact (B28) si disponible, sinon l'unique primitive d'avant (prochaine occurrence)."""
    version = _occ(dsn, occ)["version"]

    async def fn(svc, phone):
        if _HAS_EXACT:
            return await svc.refresh_recurring_need_matching(phone, str(w["need"]), str(occ), version)
        return await svc.refresh_recurring_need_matching(phone, str(w["need"]))

    return _call(dsn, w["g"], fn)


def _fail_by_timeout(dsn, w):
    """Timeout RÉEL du service (B14), sur une livraison encore dans le futur : la commande est datée dans le passé comme le fait
    le test B14 (avant B28 le timeout ne regardait que la date de la commande)."""
    order = _accept(dsn, w["g"], w["need"], w["occ"])[0]
    _sql(dsn, "update marketplace.orders set expected_fulfillment_date = %s where id = %s", (_dt(TODAY - timedelta(days=1)), order))

    async def sweep(svc, phone):
        return await svc.expire_unconfirmed_recurring_orders()

    _call(dsn, w["g"], sweep)
    return order


def test_R1_a_producer_timeout_must_not_kill_a_still_deliverable_occurrence(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    _fail_by_timeout(pg_dsn, w)
    assert _occ(pg_dsn, w["occ"])["status"] == "OPEN"  # avant B28 : UNFULFILLED (occurrence perdue)


def test_R2_find_someone_else_must_rematch_the_failed_occurrence_not_the_next_one(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3), second_date=TODAY + timedelta(days=10))
    _fail_by_timeout(pg_dsn, w)
    _relaunch(pg_dsn, w, w["occ"])
    assert _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])]  # la livraison échouée est re-matchée…
    assert _allocs(pg_dsn, w["occ2"]) == []  # …et la suivante n'est PAS touchée (avant B28 : l'inverse)


def test_R3_an_explicit_producer_rejection_must_leave_a_rematch_path(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _producer_cancel(pg_dsn, w["g"], w["A"], order)
    assert _occ(pg_dsn, w["occ"])["status"] == "OPEN"  # avant B28 : UNFULFILLED, plus aucune relance possible
    _relaunch(pg_dsn, w, w["occ"])
    assert _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])]


def test_R4_the_old_proposal_is_not_actionable_after_a_rematch(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    v1 = _occ(pg_dsn, w["occ"])["version"]
    _sql(pg_dsn, "update marketplace.products set quantity_for_sale = 0 where id = %s", (str(w["A"][2]),))  # A n'a plus de stock

    async def rematch(session):
        return await NeedMatchingService(session).rematch_occurrence(w["occ"], trigger="test")

    from test_recurring_fulfillment_pg import _run

    _run(pg_dsn, rematch)
    assert _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])]

    async def old_accept(svc, phone):
        return await svc.accept_match_proposal(phone, str(w["need"]), "ACCEPT", occurrence_id=str(w["occ"]), expected_version=v1)

    assert _call(pg_dsn, w["g"], old_accept)["outcome"] == "PROPOSAL_CHANGED"
    assert _orders(pg_dsn, w["occ"]) == []


def test_R5_two_concurrent_relaunches_never_duplicate_sourcing(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    phone, buyer, prod = _ids(pg_dsn, w["g"])
    version = _occ(pg_dsn, w["occ"])["version"]

    async def one(engine):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            svc = _Svc(session, buyer, prod)
            if _HAS_EXACT:
                r = await svc.refresh_recurring_need_matching(phone, str(w["need"]), str(w["occ"]), version)
            else:
                r = await svc.refresh_recurring_need_matching(phone, str(w["need"]))
            await session.commit()
            return r

    async def go():
        engine = _engine(pg_dsn)
        try:
            return await asyncio.gather(one(engine), one(engine))
        finally:
            await engine.dispose()

    asyncio.run(go())
    assert len(_proposed(pg_dsn, w["occ"])) == 1 and _sql(
        pg_dsn, "select count(*) from marketplace.need_allocations where occurrence_id = %s", (str(w["occ"]),))[0][0] == 1


def test_R6_a_late_producer_confirmation_cannot_resurrect_a_failed_attempt(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    order = _fail_by_timeout(pg_dsn, w)
    before = _sql(pg_dsn, "select status from marketplace.orders where id = %s", (order,))[0][0]
    assert before == "CANCELLED"
    with pytest.raises(Exception):  # noqa: B017 — refus métier (statut non confirmable)
        _confirm(pg_dsn, w["g"], w["A"], order)
    assert _sql(pg_dsn, "select status from marketplace.orders where id = %s", (order,))[0][0] == "CANCELLED"


def test_R7_list_and_detail_resolve_the_same_occurrence_after_a_failure(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3), second_date=TODAY + timedelta(days=10))
    _fail_by_timeout(pg_dsn, w)

    async def view(svc, phone):
        return await svc.list_my_recurring_needs(phone), await svc.get_recurring_need_detail(phone, str(w["need"]))

    listing, detail = _call(pg_dsn, w["g"], view)
    item = [i for i in listing["items"] if i["recurring_need_id"] == str(w["need"])][0]
    assert item["next_occurrence_id"] == detail["occurrence_id"] and item["next_occurrence_date"] == detail["occurrence_date"]


def test_R8_availability_is_attached_to_the_occurrence_that_failed_not_to_the_next(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3), second_date=TODAY + timedelta(days=10))
    _fail_by_timeout(pg_dsn, w)
    _relaunch(pg_dsn, w, w["occ"])

    async def view(svc, phone):
        return await svc.get_recurring_need_detail(phone, str(w["need"]))

    detail = _call(pg_dsn, w["g"], view)
    # la prochaine livraison à couvrir est la livraison échouée : la disponibilité affichée est la sienne
    assert detail["occurrence_id"] == str(w["occ"]) and detail["quantity_matched"] == 40.0
