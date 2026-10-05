"""B28 — RÉCUPÉRATION D'UNE OCCURRENCE récurrente, contre un VRAI PostgreSQL (`pg_dsn` : base fraîche, migrations officielles).

Chaque test appelle le code de production : acceptation exacte (B26), confirmation/refus/annulation producteur, service de timeout
producteur (`expire_unconfirmed_recurring_orders`), matching réel (`NeedMatchingService`), primitive de récupération
(`recover_occurrence_sourcing`), listes/détails. Aucune charge utile d'outbox n'est insérée à la main : elle vient des services d'échec.

Un besoin = une demande durable ; une occurrence = UNE livraison ; une allocation/commande = UNE tentative de la remplir.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import psycopg2
import pytest
from factories import Graph
from sqlalchemy.ext.asyncio import AsyncSession
from test_recurring_fulfillment_pg import (
    _accept,
    _call,
    _confirm,
    _dt,
    _engine,
    _ids,
    _occ,
    _producer_cancel,
    _run,
    _sql,
    _Svc,
)

from ladini.services.database.errors import BusinessRuleException
from ladini.workers.automation.need_matching_service import NeedMatchingService

TODAY = date.today()


# ───────────────────────────── monde de test ─────────────────────────────────────────────────────────────────────────
def _world(dsn, d: date, *, requested=40, stock_a=100.0, stock_b=100.0, price_a=500.0, price_b=600.0, second_date: Optional[date] = None):
    """Un besoin, UNE occurrence `MATCHED` (proposition du producteur A), un 2e producteur B disponible dans la même
    sous-catégorie. `second_date` : une 2e occurrence `OPEN` du même besoin (pour prouver qu'elle n'est jamais touchée)."""
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        need = g.recurring_need(quantity=requested, unit="KG", recurrence_type="DAILY", starts_at=_dt(d))
        occ = g.occurrence(need, occurrence_date=_dt(d), requested_quantity=requested, unit="KG", status="MATCHED",
                           quantity_matched=requested, notified_at=datetime.utcnow())
        a_prod, a_user = g.producer, g.producer_user
        a_product = g.product_for(producer=a_prod, quantity_for_sale=stock_a, price=price_a)
        g.allocation(occurrence=occ, producer=a_prod, product=a_product, quantity=requested, unit_price=price_a, unit="KG")
        b_prod = g.extra_producer()
        b_user = _sql_user_of(cur, b_prod)
        b_product = g.product_for(producer=b_prod, quantity_for_sale=stock_b, price=price_b)
        occ2 = g.occurrence(need, occurrence_date=_dt(second_date), requested_quantity=requested, unit="KG", status="OPEN") if second_date else None
    conn.close()
    return {"g": g, "need": need, "occ": occ, "occ2": occ2, "A": (a_prod, a_user, a_product), "B": (b_prod, b_user, b_product)}


def _sql_user_of(cur, producer):
    cur.execute("select user_id from marketplace.producers where id = %s", (str(producer),))
    return cur.fetchone()[0]


def _allocs(dsn, occ) -> List[tuple]:
    return _sql(dsn, "select producer_id::text, status, quantity from marketplace.need_allocations where occurrence_id = %s "
                     "order by created_at, producer_id", (str(occ),))


def _proposed(dsn, occ) -> List[str]:
    return [p for p, st, _q in _allocs(dsn, occ) if st == "PROPOSED"]


def _orders(dsn, occ) -> List[tuple]:
    return _sql(dsn, "select distinct o.id::text, o.status, o.cancellation_role from marketplace.orders o "
                     "join marketplace.order_items oi on oi.order_id = o.id "
                     "join marketplace.need_allocations na on na.order_item_id = oi.id where na.occurrence_id = %s order by 1", (str(occ),))


def _need_status(dsn, need) -> str:
    return _sql(dsn, "select status from marketplace.recurring_needs where id = %s", (str(need),))[0][0]


def _recover(dsn, w, *, version=None, occ=None, source="USER", producer=None):
    occ = occ or w["occ"]
    version = _occ(dsn, occ)["version"] if version is None else version

    async def fn(svc, phone):
        return await svc.recover_occurrence_sourcing(phone, str(w["need"]), str(occ), version, source=source)

    return _call(dsn, w["g"], fn)


def _timeout(dsn, w, monkeypatch, *, lead_days=1):
    from ladini.core.settings import settings

    monkeypatch.setattr(settings, "RECURRING_PRODUCER_CONFIRMATION_LEAD_DAYS", lead_days)

    async def sweep(svc, phone):
        return await svc.expire_unconfirmed_recurring_orders()

    return _call(dsn, w["g"], sweep)


def _buyer_notifications(dsn, w) -> List[Dict[str, Any]]:
    phone = _sql(dsn, "select phone from auth.users where id = %s", (str(w["g"].buyer_user),))[0][0]
    rows = _sql(dsn, "select payload from intelligence.notification_outbox where recipient_phone = %s "
                     "and template_key = 'ORDER_CANCELLED_BY_PRODUCER_BUYER' order by created_at", (phone,))
    return [r[0] for r in rows]


def _failed_world(dsn, monkeypatch, *, d: Optional[date] = None, **kw):
    """Parcours réel jusqu'à l'échec : acceptation exacte -> commande en attente producteur -> service de timeout."""
    w = _world(dsn, d or TODAY, **kw)
    w["order"] = _accept(dsn, w["g"], w["need"], w["occ"])[0]
    w["timeout"] = _timeout(dsn, w, monkeypatch)
    return w


# ═══════════════════════ 1. TIMEOUT PRODUCTEUR RÉCUPÉRABLE (cas principal) ════════════════════════════════════════════
def test_producer_timeout_keeps_the_occurrence_alive_and_notifies_with_the_domain_verdict(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch, second_date=TODAY + timedelta(days=7))
    assert w["timeout"]["recurring_orders_expired"] >= 1
    # tentative échouée = historique ; l'occurrence n'est PAS perdue
    assert _orders(pg_dsn, w["occ"]) == [(w["order"], "CANCELLED", "SYSTEM")]
    assert [st for _p, st, _q in _allocs(pg_dsn, w["occ"])] == ["CONVERTED"]
    after = _occ(pg_dsn, w["occ"])
    assert after["status"] == "OPEN" and after["confirmed"] == 0.0 and after["version"] > 1
    assert _need_status(pg_dsn, w["need"]) == "ACTIVE"
    # la notification vient du service d'échec : verdict du domaine + identifiants + version + raison structurée
    payload = _buyer_notifications(pg_dsn, w)[-1]
    assert payload["recovery_candidate"] is True and payload["recovery_outcome"] == "RECOVERABLE"
    assert payload["failure_reason"] == "PRODUCER_TIMEOUT"
    entry = payload["occurrences"][0]
    assert entry["occurrence_id"] == str(w["occ"]) and entry["occurrence_version"] == after["version"]
    from ladini.workers.outbox.templates import render

    text = render("ORDER_CANCELLED_BY_PRODUCER_BUYER", payload)
    assert "reste à couvrir" in text and "même livraison" in text and "chercher <produit>" not in text


def test_recovery_rematches_the_same_occurrence_with_another_producer_and_keeps_history(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch, second_date=TODAY + timedelta(days=7))
    next_before = _occ(pg_dsn, w["occ2"])
    failed_version = _occ(pg_dsn, w["occ"])["version"]
    res = _recover(pg_dsn, w)
    assert res["outcome"] == "RECOVERY_APPLIED" and res["proposal_available"] is True and res["occurrence_id"] == str(w["occ"])
    # MÊME occurrence re-matchée ; producteur échoué écarté ; nouveau producteur proposé
    assert _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])]
    assert (str(w["A"][0]), "CONVERTED") in [(p, st) for p, st, _q in _allocs(pg_dsn, w["occ"])]  # historique intact
    after = _occ(pg_dsn, w["occ"])
    assert after["status"] == "MATCHED" and after["version"] > failed_version
    # la vieille commande reste historique, aucune nouvelle commande n'est créée par le rematch (pas d'auto-acceptation)
    assert _orders(pg_dsn, w["occ"]) == [(w["order"], "CANCELLED", "SYSTEM")]
    # l'occurrence suivante n'a PAS été touchée
    assert _occ(pg_dsn, w["occ2"]) == next_before and _allocs(pg_dsn, w["occ2"]) == []
    assert _need_status(pg_dsn, w["need"]) == "ACTIVE"
    # une nouvelle acceptation produit une NOUVELLE commande, liée à la même occurrence ; l'ancienne reste annulée
    new_order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    assert new_order != w["order"]
    assert sorted(o[1] for o in _orders(pg_dsn, w["occ"])) == ["CANCELLED", "PENDING_PRODUCER_CONFIRMATION"]
    assert _occ(pg_dsn, w["occ"])["status"] == "ACCEPTED"


def test_the_failed_producer_is_excluded_only_for_that_occurrence(pg_dsn, monkeypatch):
    """Seul le producteur échoué existe pour cette livraison : pas de re-proposition immédiate ; il reste candidat ailleurs."""
    w = _failed_world(pg_dsn, monkeypatch, second_date=TODAY + timedelta(days=7), stock_b=0.0)
    _sql(pg_dsn, "update marketplace.products set is_available = false where id = %s", (str(w["B"][2]),))
    res = _recover(pg_dsn, w)
    assert res["outcome"] == "RECOVERY_APPLIED" and res["proposal_available"] is False
    assert _proposed(pg_dsn, w["occ"]) == []
    assert _occ(pg_dsn, w["occ"])["status"] == "OPEN"
    # pour la livraison SUIVANTE, le même producteur est toujours proposable
    async def match_next(session):
        return await NeedMatchingService(session).rematch_occurrence(w["occ2"], trigger="test")

    _run(pg_dsn, match_next)
    assert _proposed(pg_dsn, w["occ2"]) == [str(w["A"][0])]


def test_a_same_day_recovery_survives_a_late_second_failure(pg_dsn, monkeypatch):
    """Deux tentatives successives échouent : l'historique garde les deux producteurs, aucune ressuscitation."""
    w = _failed_world(pg_dsn, monkeypatch)
    assert _recover(pg_dsn, w)["outcome"] == "RECOVERY_APPLIED"
    second = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _sql(pg_dsn, "update marketplace.orders set expected_fulfillment_date = %s where id = %s", (_dt(TODAY), second))
    _timeout(pg_dsn, w, monkeypatch)
    assert _occ(pg_dsn, w["occ"])["status"] == "OPEN"
    assert sorted(st for _p, st, _q in _allocs(pg_dsn, w["occ"])) == ["CONVERTED", "CONVERTED"]
    assert [o[1] for o in _orders(pg_dsn, w["occ"])] == ["CANCELLED", "CANCELLED"]
    res = _recover(pg_dsn, w)  # plus aucun producteur non essayé
    assert res["outcome"] == "RECOVERY_APPLIED" and res["proposal_available"] is False


# ═══════════════════════ 2. TIMEOUT TROP TARD : fenêtre fermée ═══════════════════════════════════════════════════════
def test_a_timeout_after_the_delivery_date_closes_the_occurrence_honestly_and_keeps_the_need(pg_dsn, monkeypatch):
    d = TODAY - timedelta(days=1)
    w = _world(pg_dsn, TODAY, second_date=TODAY + timedelta(days=7))
    w["order"] = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    # le temps passe : la livraison d'hier n'a pas eu lieu (la date de la commande = celle de l'occurrence)
    _sql(pg_dsn, "update marketplace.orders set expected_fulfillment_date = %s where id = %s", (_dt(d), w["order"]))
    _sql(pg_dsn, "update marketplace.recurring_need_occurrences set occurrence_date = %s where id = %s", (_dt(d), str(w["occ"])))
    _timeout(pg_dsn, w, monkeypatch, lead_days=0)
    assert _occ(pg_dsn, w["occ"])["status"] == "UNFULFILLED"
    payload = _buyer_notifications(pg_dsn, w)[-1]
    assert payload["recovery_outcome"] == "RECOVERY_WINDOW_CLOSED"
    from ladini.workers.outbox.templates import render

    text = render("ORDER_CANCELLED_BY_PRODUCER_BUYER", payload)
    assert "ne peut plus être relancée à temps" in text and "reste actif pour les prochaines livraisons" in text
    # la commande de relance est refusée par le DOMAINE, sans aucun effet
    res = _recover(pg_dsn, w)
    assert res["outcome"] in ("NOT_RECOVERABLE", "RECOVERY_WINDOW_CLOSED")
    assert _proposed(pg_dsn, w["occ"]) == [] and _need_status(pg_dsn, w["need"]) == "ACTIVE"
    # le besoin continue : l'occurrence suivante existe toujours et reste cherchable
    assert _occ(pg_dsn, w["occ2"])["status"] == "OPEN"


def test_the_default_confirmation_deadline_is_the_delivery_date_itself_B14_unchanged(pg_dsn, monkeypatch):
    """Lead 0 (défaut) : une commande pour AUJOURD'HUI n'expire pas aujourd'hui — règle B14 inchangée."""
    w = _world(pg_dsn, TODAY)
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _timeout(pg_dsn, w, monkeypatch, lead_days=0)
    assert _sql(pg_dsn, "select status from marketplace.orders where id = %s", (order,))[0][0] == "PENDING_PRODUCER_CONFIRMATION"
    assert _occ(pg_dsn, w["occ"])["status"] == "ACCEPTED"


# ═══════════════════════ 3. REFUS / ANNULATION PRODUCTEUR ════════════════════════════════════════════════════════════
def test_explicit_producer_rejection_before_commitment_is_recoverable(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=2), second_date=TODAY + timedelta(days=9))
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    assert _producer_cancel(pg_dsn, w["g"], w["A"], order)["outcome"] == "CANCELLED"
    assert _occ(pg_dsn, w["occ"])["status"] == "OPEN"
    payload = _buyer_notifications(pg_dsn, w)[-1]
    assert payload["failure_reason"] == "PRODUCER_REJECTED" and payload["recovery_outcome"] == "RECOVERABLE"
    res = _recover(pg_dsn, w)
    assert res["outcome"] == "RECOVERY_APPLIED" and _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])]
    assert _allocs(pg_dsn, w["occ2"]) == []


def test_producer_cancellation_after_confirmation_is_recoverable_while_the_window_is_open(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=2))
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _confirm(pg_dsn, w["g"], w["A"], order)
    assert _occ(pg_dsn, w["occ"])["status"] == "ACCEPTED"  # engagé : pas récupérable tant que la commande vit
    res = _recover(pg_dsn, w)
    assert res["outcome"] == "OCCURRENCE_COMMITTED" and _proposed(pg_dsn, w["occ"]) == []
    _producer_cancel(pg_dsn, w["g"], w["A"], order)
    assert _occ(pg_dsn, w["occ"])["status"] == "OPEN"
    assert _buyer_notifications(pg_dsn, w)[-1]["failure_reason"] == "PRODUCER_CANCELLED"


def test_a_committed_occurrence_is_never_rematched(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=2))
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _confirm(pg_dsn, w["g"], w["A"], order)
    before = _allocs(pg_dsn, w["occ"])
    assert _recover(pg_dsn, w)["outcome"] == "OCCURRENCE_COMMITTED"
    assert _allocs(pg_dsn, w["occ"]) == before and _occ(pg_dsn, w["occ"])["status"] == "ACCEPTED"


# ═══════════════════════ 4. ALLOCATION EXPIRÉE / PROPOSITION REFUSÉE / QUANTITÉ ══════════════════════════════════════
def test_allocation_expired_by_a_quantity_change_is_rematched_on_the_same_occurrence(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3), requested=40)
    version = _occ(pg_dsn, w["occ"])["version"]

    async def mutate(svc, phone):
        need_version = (await svc.list_my_recurring_needs(phone))["items"][0]["need_version"]
        return await svc.update_recurring_need(phone, str(w["need"]), "PERMANENT_QUANTITY", quantity=60, expected_version=need_version)

    _call(pg_dsn, w["g"], mutate)
    mid = _occ(pg_dsn, w["occ"])
    assert mid["status"] == "OPEN" and mid["version"] > version  # allocation expirée, compteurs remis à zéro
    assert [st for _p, st, _q in _allocs(pg_dsn, w["occ"])] == ["EXPIRED"]
    res = _recover(pg_dsn, w)
    assert res["outcome"] == "RECOVERY_APPLIED" and res["quantity_matched"] == 60.0  # la quantité COURANTE de l'occurrence


def test_a_rejected_proposal_is_recoverable_on_explicit_request_only(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    v = _occ(pg_dsn, w["occ"])["version"]

    async def reject(svc, phone):
        return await svc.accept_match_proposal(phone, str(w["need"]), "REJECT", occurrence_id=str(w["occ"]), expected_version=v)

    _call(pg_dsn, w["g"], reject)
    assert _occ(pg_dsn, w["occ"])["status"] == "REJECTED"
    assert _recover(pg_dsn, w, source="CRON")["outcome"] == "NOT_RECOVERABLE"  # jamais automatique
    assert _occ(pg_dsn, w["occ"])["status"] == "REJECTED"
    res = _recover(pg_dsn, w)  # demande explicite de l'acheteur : « pas celui-là, cherche un autre »
    assert res["outcome"] == "RECOVERY_APPLIED" and _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])]  # producteur refusé écarté


def test_override_quantity_is_preserved_through_recovery(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3), requested=5)
    _sql(pg_dsn, "update marketplace.recurring_needs set quantity = 2 where id = %s", (str(w["need"]),))  # besoin = 2, occurrence = 5
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _producer_cancel(pg_dsn, w["g"], w["A"], order)
    res = _recover(pg_dsn, w)
    assert res["requested_quantity"] == 5.0 and res["quantity_matched"] == 5.0
    assert [float(q) for p, st, q in _allocs(pg_dsn, w["occ"]) if st == "PROPOSED"] == [5.0]


def test_a_later_permanent_update_never_rewrites_a_failed_delivery_silently(pg_dsn, monkeypatch):
    """Besoin passé à 3 pendant que la livraison (snapshot 40) est en échec : on cherche la quantité DE L'OCCURRENCE réouverte
    tant qu'elle n'a pas été réécrite par une mutation explicite (B25) — jamais un mélange implicite."""
    w = _failed_world(pg_dsn, monkeypatch, requested=40)
    _sql(pg_dsn, "update marketplace.recurring_needs set quantity = 3 where id = %s", (str(w["need"]),))
    res = _recover(pg_dsn, w)
    assert res["requested_quantity"] == 40.0


def test_partial_availability_recovery_keeps_allocations_within_the_remaining_request(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3), requested=100, stock_a=100.0, stock_b=60.0)
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _producer_cancel(pg_dsn, w["g"], w["A"], order)
    res = _recover(pg_dsn, w)
    assert res["outcome"] == "RECOVERY_APPLIED" and res["quantity_matched"] == 60.0
    proposed = [float(q) for _p, st, q in _allocs(pg_dsn, w["occ"]) if st == "PROPOSED"]
    assert proposed == [60.0] and sum(proposed) <= 100.0
    assert _occ(pg_dsn, w["occ"])["status"] == "OPEN"  # couverture partielle : pas MATCHED


# ═══════════════════════ 5. VERSIONS : proposition périmée, ancienne acceptation ═════════════════════════════════════
def test_an_old_screen_cannot_accept_after_the_occurrence_was_rematched(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch)
    v_failed = _occ(pg_dsn, w["occ"])["version"]
    assert _recover(pg_dsn, w)["outcome"] == "RECOVERY_APPLIED"
    orders_before = _orders(pg_dsn, w["occ"])

    async def old_accept(svc, phone):  # l'écran affichait l'ancienne version (producteur A)
        return await svc.accept_match_proposal(phone, str(w["need"]), "ACCEPT", occurrence_id=str(w["occ"]), expected_version=v_failed)

    res = _call(pg_dsn, w["g"], old_accept)
    assert res["outcome"] == "PROPOSAL_CHANGED"
    assert _orders(pg_dsn, w["occ"]) == orders_before  # aucune commande ni stock débité par l'ancien écran


def test_a_stale_recovery_command_is_a_version_conflict_not_a_blind_mutation(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _producer_cancel(pg_dsn, w["g"], w["A"], order)
    stale = _occ(pg_dsn, w["occ"])["version"] - 1  # l'écran de la notification précède un changement
    before = (_occ(pg_dsn, w["occ"]), _allocs(pg_dsn, w["occ"]))
    res = _recover(pg_dsn, w, version=stale)
    assert res["outcome"] in ("ALREADY_RECOVERED", "VERSION_CONFLICT")
    assert (_occ(pg_dsn, w["occ"]), _allocs(pg_dsn, w["occ"])) == before


def test_a_stale_command_on_a_closed_occurrence_is_a_version_conflict(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    order = _accept(pg_dsn, w["g"], w["need"], w["occ"])[0]
    _confirm(pg_dsn, w["g"], w["A"], order)
    res = _recover(pg_dsn, w, version=1)
    assert res["outcome"] == "VERSION_CONFLICT" and _occ(pg_dsn, w["occ"])["status"] == "ACCEPTED"


def test_a_recovery_command_without_a_version_is_refused(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))

    async def fn(svc, phone):
        return await svc.recover_occurrence_sourcing(phone, str(w["need"]), str(w["occ"]), None)

    with pytest.raises(BusinessRuleException) as err:
        _call(pg_dsn, w["g"], fn)
    assert getattr(err.value, "reason", None) == "version_required"


# ═══════════════════════ 6. IDEMPOTENCE ET CONCURRENCE ═══════════════════════════════════════════════════════════════
def test_recovering_twice_is_one_effective_recovery(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch)
    failed_version = _occ(pg_dsn, w["occ"])["version"]
    first = _recover(pg_dsn, w, version=failed_version)
    second = _recover(pg_dsn, w, version=failed_version)  # même message, même écran
    assert first["outcome"] == "RECOVERY_APPLIED" and second["outcome"] == "ALREADY_RECOVERED"
    assert len(_proposed(pg_dsn, w["occ"])) == 1 and len(_orders(pg_dsn, w["occ"])) == 1
    assert _sql(pg_dsn, "select count(*) from marketplace.recurring_need_occurrences where recurring_need_id = %s", (str(w["need"]),))[0][0] == 1


def test_two_concurrent_recoveries_produce_one_effective_transition(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch)
    failed_version = _occ(pg_dsn, w["occ"])["version"]
    phone, buyer, prod = _ids(pg_dsn, w["g"])

    async def one(engine):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            r = await _Svc(session, buyer, prod).recover_occurrence_sourcing(phone, str(w["need"]), str(w["occ"]), failed_version)
            await session.commit()
            return r["outcome"]

    async def go():
        engine = _engine(pg_dsn)
        try:
            return await asyncio.gather(one(engine), one(engine))
        finally:
            await engine.dispose()

    outcomes = sorted(asyncio.run(go()))
    assert outcomes == ["ALREADY_RECOVERED", "RECOVERY_APPLIED"], outcomes
    assert _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])] and len(_allocs(pg_dsn, w["occ"])) == 2
    assert len(_orders(pg_dsn, w["occ"])) == 1


def test_cron_matching_and_user_recovery_never_duplicate_sourcing(pg_dsn, monkeypatch):
    """Le cron rematch (même moteur) en même temps que « cherche quelqu'un d'autre » : une seule allocation vivante."""
    w = _failed_world(pg_dsn, monkeypatch)
    failed_version = _occ(pg_dsn, w["occ"])["version"]
    phone, buyer, prod = _ids(pg_dsn, w["g"])

    async def user(engine):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            return (await _Svc(session, buyer, prod).recover_occurrence_sourcing(phone, str(w["need"]), str(w["occ"]), failed_version))["outcome"]

    async def cron(engine):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            report = await NeedMatchingService(session).match_upcoming_occurrences(within_hours=72, trigger="cron_test")
            return report.occurrences_examined

    async def go():
        engine = _engine(pg_dsn)
        try:
            return await asyncio.gather(user(engine), cron(engine))
        finally:
            await engine.dispose()

    outcome, _examined = asyncio.run(go())
    assert outcome in ("RECOVERY_APPLIED", "ALREADY_RECOVERED"), outcome
    assert _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])]
    assert sorted(st for _p, st, _q in _allocs(pg_dsn, w["occ"])) == ["CONVERTED", "PROPOSED"]
    assert len(_orders(pg_dsn, w["occ"])) == 1


def test_the_reopened_occurrence_is_rematched_by_the_regular_cron_with_the_same_exclusion(pg_dsn, monkeypatch):
    """Convergence cron/self-service : un seul moteur, une seule règle d'exclusion — et l'utilisateur voit l'état déjà récupéré."""
    w = _failed_world(pg_dsn, monkeypatch)
    failed_version = _occ(pg_dsn, w["occ"])["version"]

    async def cron(session):
        return await NeedMatchingService(session).match_upcoming_occurrences(within_hours=72, trigger="scheduled")

    _run(pg_dsn, cron)
    assert _proposed(pg_dsn, w["occ"]) == [str(w["B"][0])]
    assert _recover(pg_dsn, w, version=failed_version)["outcome"] == "ALREADY_RECOVERED"


# ═══════════════════════ 7. PRODUCTEUR TARDIF ════════════════════════════════════════════════════════════════════════
def test_a_late_producer_confirmation_cannot_resurrect_a_failed_attempt(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch)
    assert _recover(pg_dsn, w)["outcome"] == "RECOVERY_APPLIED"
    before = (_occ(pg_dsn, w["occ"]), _allocs(pg_dsn, w["occ"]), _orders(pg_dsn, w["occ"]))
    stock = float(_sql(pg_dsn, "select quantity_for_sale from marketplace.products where id = %s", (str(w["A"][2]),))[0][0])
    with pytest.raises(BusinessRuleException):
        _confirm(pg_dsn, w["g"], w["A"], w["order"])  # le producteur A confirme TARD
    assert (_occ(pg_dsn, w["occ"]), _allocs(pg_dsn, w["occ"]), _orders(pg_dsn, w["occ"])) == before
    assert float(_sql(pg_dsn, "select quantity_for_sale from marketplace.products where id = %s", (str(w["A"][2]),))[0][0]) == stock


# ═══════════════════════ 8. PAUSE / ANNULATION / SKIP / FENÊTRE ══════════════════════════════════════════════════════
@pytest.mark.parametrize("action,kw,expected", [
    ("PAUSE", {"paused_until": (TODAY + timedelta(days=30)).isoformat()}, "NEED_NOT_ACTIVE"),
    ("CANCEL", {}, "NEED_NOT_ACTIVE"),
])
def test_recovery_stops_when_the_need_is_paused_or_cancelled(pg_dsn, monkeypatch, action, kw, expected):
    w = _failed_world(pg_dsn, monkeypatch, second_date=TODAY + timedelta(days=7))
    v = _occ(pg_dsn, w["occ"])["version"]

    async def mutate(svc, phone):
        need_version = (await svc.list_my_recurring_needs(phone))["items"][0]["need_version"]
        return await svc.update_recurring_need(phone, str(w["need"]), action, expected_version=need_version, **kw)

    _call(pg_dsn, w["g"], mutate)
    before = _allocs(pg_dsn, w["occ"])
    res = _recover(pg_dsn, w, version=_occ(pg_dsn, w["occ"])["version"])
    assert res["outcome"] == expected, (res, v)
    assert _allocs(pg_dsn, w["occ"]) == before and _proposed(pg_dsn, w["occ"]) == []


def test_recovery_is_refused_on_a_skipped_occurrence(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    _sql(pg_dsn, "update marketplace.recurring_need_occurrences set status = 'SKIPPED' where id = %s", (str(w["occ"]),))
    assert _recover(pg_dsn, w)["outcome"] == "NOT_RECOVERABLE"
    assert _occ(pg_dsn, w["occ"])["status"] == "SKIPPED"


@pytest.mark.parametrize("status", ["FULFILLED", "CANCELLED", "PARTIALLY_FULFILLED"])
def test_a_stale_recovery_candidate_on_a_terminal_occurrence_is_refused(pg_dsn, status):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    _sql(pg_dsn, "update marketplace.recurring_need_occurrences set status = %s where id = %s", (status, str(w["occ"])))
    assert _recover(pg_dsn, w)["outcome"] == "NOT_RECOVERABLE"
    assert _occ(pg_dsn, w["occ"])["status"] == status


def test_an_unfulfilled_occurrence_after_a_buyer_cancellation_is_not_recoverable(pg_dsn):
    w = _world(pg_dsn, TODAY + timedelta(days=3))
    _sql(pg_dsn, "update marketplace.recurring_need_occurrences set status = 'UNFULFILLED' where id = %s", (str(w["occ"]),))
    assert _recover(pg_dsn, w)["outcome"] == "NOT_RECOVERABLE"


def test_a_past_occurrence_never_reopens_and_the_next_one_is_never_substituted(pg_dsn):
    """Occurrence d'hier encore ouverte (cron d'expiration pas encore passé) : clôture honnête, jamais la suivante."""
    yesterday = TODAY - timedelta(days=1)
    w = _world(pg_dsn, yesterday, second_date=TODAY + timedelta(days=6))
    next_before = (_occ(pg_dsn, w["occ2"]), _allocs(pg_dsn, w["occ2"]))
    res = _recover(pg_dsn, w)
    assert res["outcome"] == "RECOVERY_WINDOW_CLOSED" and res["occurrence_id"] == str(w["occ"])
    assert _occ(pg_dsn, w["occ"])["status"] == "EXPIRED"
    assert (_occ(pg_dsn, w["occ2"]), _allocs(pg_dsn, w["occ2"])) == next_before
    assert _need_status(pg_dsn, w["need"]) == "ACTIVE"


def test_recovering_the_failed_day_never_touches_the_next_occurrence(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch, second_date=TODAY + timedelta(days=7))
    next_before = (_occ(pg_dsn, w["occ2"]), _allocs(pg_dsn, w["occ2"]))
    _recover(pg_dsn, w)
    assert (_occ(pg_dsn, w["occ2"]), _allocs(pg_dsn, w["occ2"])) == next_before
    # et la relance « de la prochaine » (comportement historique de « rechercher maintenant ») ne touche pas la livraison échouée
    ids = _sql(pg_dsn, "select id::text, occurrence_date::date from marketplace.recurring_need_occurrences where recurring_need_id = %s order by 2", (str(w["need"]),))
    assert [i for i, _d in ids] == [str(w["occ"]), str(w["occ2"])]


# ═══════════════════════ 9. PROPRIÉTÉ ════════════════════════════════════════════════════════════════════════════════
def test_another_buyer_can_never_recover_someone_elses_occurrence(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch)
    other = _world(pg_dsn, TODAY + timedelta(days=4))  # un autre acheteur, un autre besoin
    v = _occ(pg_dsn, w["occ"])["version"]

    async def fn(svc, phone):  # le téléphone de l'autre acheteur vise l'occurrence d'un étranger
        return await svc.recover_occurrence_sourcing(phone, str(w["need"]), str(w["occ"]), v)

    res = _call(pg_dsn, other["g"], fn)
    assert res["outcome"] == "TARGET_NOT_FOUND"
    assert _occ(pg_dsn, w["occ"])["version"] == v
    assert _recover(pg_dsn, w, occ=other["occ"])["outcome"] == "TARGET_NOT_FOUND"  # mauvaise occurrence pour ce besoin


# ═══════════════════════ 10. DEUX ÉCHECS SIMULTANÉS, LISTE/DÉTAIL, CONTEXTE ══════════════════════════════════════════
def _second_failed_delivery(dsn, w, monkeypatch, *, qty=20):
    """Une 2e livraison (autre besoin du MÊME acheteur) qui échoue aussi."""
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as cur:
        g = w["g"]
        g.cur = cur
        sub = g.sub_category
        need2 = g.recurring_need(quantity=qty, unit="KG", recurrence_type="DAILY", starts_at=_dt(TODAY))
        occ2 = g.occurrence(need2, occurrence_date=_dt(TODAY), requested_quantity=qty, unit="KG", status="MATCHED",
                            quantity_matched=qty, notified_at=datetime.utcnow())
        prod = g.extra_producer()
        product = g.product_for(producer=prod, quantity_for_sale=100.0, price=450.0)
        g.allocation(occurrence=occ2, producer=prod, product=product, quantity=qty, unit_price=450.0, unit="KG")
    conn.close()
    del sub
    order = _accept(dsn, g, need2, occ2)[0]
    _timeout(dsn, w, monkeypatch)
    return {"need": need2, "occ": occ2, "order": order, "producer": (prod, _sql(dsn, "select user_id from marketplace.producers where id=%s", (str(prod),))[0][0], product)}


def test_two_simultaneous_failures_keep_two_distinct_recovery_contexts(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch)
    w2 = _second_failed_delivery(pg_dsn, w, monkeypatch)
    assert w["order"] != w2["order"] and w["occ"] != w2["occ"]
    notifications = _buyer_notifications(pg_dsn, w)
    assert len(notifications) == 2
    assert {n["occurrences"][0]["occurrence_id"] for n in notifications} == {str(w["occ"]), str(w2["occ"])}
    # le contexte lu par la conversation les contient TOUS LES DEUX (aucun écrasement) ; la relance est ciblée par occurrence
    old_phone = _sql(pg_dsn, "select phone from auth.users where id = %s", (str(w["g"].buyer_user),))[0][0]
    phone = "+226" + str(uuid.uuid4().int)[:8]  # un numéro normalisable : la lecture de contexte passe par `normalize_phone`
    _sql(pg_dsn, "update auth.users set phone = %s where id = %s", (phone, str(w["g"].buyer_user)))
    _sql(pg_dsn, "update intelligence.notification_outbox set status='SENT', sent_at = now() at time zone 'UTC', "
                 "recipient_phone = %s where recipient_phone = %s", (phone, old_phone))

    async def ctx(session):
        return await _Svc(session, None, None).get_last_interactive_outbound(phone)

    rec = _run(pg_dsn, ctx)["recovery"]
    assert sorted(rec["occurrence_ids"]) == sorted([str(w["occ"]), str(w2["occ"])]) and len(rec["recurring_need_ids"]) == 2
    assert all(v is not None for v in rec["occurrence_versions"])
    assert set(rec["reasons"]) == {"PRODUCER_TIMEOUT"}
    # chacune se récupère indépendamment
    assert _recover(pg_dsn, w)["outcome"] == "RECOVERY_APPLIED"
    assert _occ(pg_dsn, w2["occ"])["status"] == "OPEN" and _proposed(pg_dsn, w2["occ"]) == []
    w2["g"], w2["need"] = w["g"], w2["need"]
    assert _recover(pg_dsn, w2)["outcome"] == "RECOVERY_APPLIED"


def test_list_and_detail_describe_the_same_recovered_occurrence(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch, second_date=TODAY + timedelta(days=7))
    assert _recover(pg_dsn, w)["outcome"] == "RECOVERY_APPLIED"

    async def view(svc, phone):
        listing = await svc.list_my_recurring_needs(phone)
        detail = await svc.get_recurring_need_detail(phone, str(w["need"]))
        return listing, detail

    listing, detail = _call(pg_dsn, w["g"], view)
    item = [i for i in listing["items"] if i["recurring_need_id"] == str(w["need"])][0]
    assert item["next_occurrence_id"] == detail["occurrence_id"] == str(w["occ"])  # la MÊME livraison (celle qui a échoué puis récupérée)
    assert item["next_occurrence_date"] == detail["occurrence_date"] == TODAY.isoformat()
    assert item["next_occurrence_version"] == detail["occurrence_version"]
    assert item["matched_quantity"] == detail["quantity_matched"] == 40.0  # disponibilité attachée à CETTE occurrence
    labels = [a["producer_label"] for a in detail["allocations"]]
    assert len(labels) == 1  # seule la NOUVELLE proposition est affichée : l'ancienne allocation convertie est de l'historique
    assert detail["orders"] == []  # aucune commande vivante


def test_the_onion_case_list_and_detail_agree_on_the_same_far_occurrence(pg_dsn):
    """« Oignon — prochaine livraison 27 octobre — disponibilité 75/75 KG » : la liste et le détail parlent de la MÊME occurrence."""
    far = TODAY + timedelta(days=22)
    w = _world(pg_dsn, far, requested=75)

    async def view(svc, phone):
        return await svc.list_my_recurring_needs(phone), await svc.get_recurring_need_detail(phone, str(w["need"]))

    listing, detail = _call(pg_dsn, w["g"], view)
    item = [i for i in listing["items"] if i["recurring_need_id"] == str(w["need"])][0]
    assert (item["next_occurrence_id"], item["next_occurrence_date"]) == (detail["occurrence_id"], detail["occurrence_date"]) == (str(w["occ"]), far.isoformat())
    assert (item["requested_quantity"], item["matched_quantity"]) == (75.0, 75.0) == (detail["requested_quantity"], detail["quantity_matched"])


# ═══════════════════════ 11. ALLOCATIONS COHÉRENTES ══════════════════════════════════════════════════════════════════
def test_live_allocations_never_exceed_the_requested_quantity_after_recovery(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch, requested=40)
    _recover(pg_dsn, w)
    live = [float(q) for _p, st, q in _allocs(pg_dsn, w["occ"]) if st == "PROPOSED"]
    assert sum(live) <= 40.0
    row = _occ(pg_dsn, w["occ"])
    assert float(_sql(pg_dsn, "select quantity_matched from marketplace.recurring_need_occurrences where id=%s", (str(w["occ"]),))[0][0]) == sum(live)
    assert row["confirmed"] == 0.0


def test_a_failed_attempt_cannot_be_accepted_again(pg_dsn, monkeypatch):
    w = _failed_world(pg_dsn, monkeypatch)
    v = _occ(pg_dsn, w["occ"])["version"]

    async def accept(svc, phone):
        return await svc.accept_match_proposal(phone, str(w["need"]), "ACCEPT", occurrence_id=str(w["occ"]), expected_version=v)

    res = _call(pg_dsn, w["g"], accept)
    assert res["outcome"] in ("NO_PROPOSAL", "PROPOSAL_CHANGED"), res  # rien à accepter : l'ancienne allocation est convertie/historique
    assert len(_orders(pg_dsn, w["occ"])) == 1
