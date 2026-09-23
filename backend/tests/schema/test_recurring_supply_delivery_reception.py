"""`RecurringSupplyMixin.mark_order_delivery_status`/`record_order_reception` contre un vrai
PostgreSQL (VS5 pilote) — même idiome que `test_recurring_need_confirmation_service.py` : une
occurrence déjà `MATCHED`+allocation `PROPOSED` (matching déjà éprouvé ailleurs), acceptée via
`accept_match_proposal` (déjà éprouvé, commit 9c68dc3) pour obtenir une VRAIE commande
`RECURRING_SUPPLY`, puis le cycle BESOIN → ... → CONFIRMATION PRODUCTEUR → LIVRAISON → RÉCEPTION.

Couvre les CAS 1 à 12 du mandat §14 (transitions valides/invalides, idempotence, ownership,
non-régression du flux historique préorder), plus (§15) un scénario d'intégration complet en deux
variantes : "tout est bon" et "réception avec quantité incorrecte"."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import psycopg2
import pytest
from factories import Graph, insert, uniq
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ladini.domain.models import Order, OrderStatusHistory
from ladini.services.database.auction import AuctionMixin
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.moderation import ModerationMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.recurring_supply import RecurringSupplyMixin


class _Svc(RecurringSupplyMixin, AuctionMixin, ModerationMixin, ProducerMgmtMixin):
    """Mêmes mixins que `test_recurring_need_confirmation_service.py::_Svc`, avec les DEUX profils
    stubés (`get_buyer_profile`/`get_producer_profile`) — ce fichier joue tantôt le rôle acheteur,
    tantôt le rôle producteur, jamais les deux en même temps sur une même instance (chacune n'a
    qu'UNE identité active, choisie à la construction)."""

    def __init__(self, session, user):
        self._s = session
        self._user = user

    @property
    def session(self):
        return self._s

    async def get_buyer_profile(self, phone):
        return self._user, self._user_profile

    async def get_producer_profile(self, phone):
        return self._user, self._user_profile


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


@pytest.fixture
def market(pg_dsn):
    """Acheteur + producteur, une occurrence `MATCHED` déjà allouée — même graphe de départ que
    `test_recurring_need_confirmation_service.py::market`."""
    conn = psycopg2.connect(pg_dsn)
    with conn, conn.cursor() as cur:
        g = Graph(cur)
        buyer_user = SimpleNamespace(id=g.buyer_user)
        buyer_profile = SimpleNamespace(id=g.buyer)
        producer_user = SimpleNamespace(id=g.producer_user)
        producer_profile = SimpleNamespace(id=g.producer)
        tomorrow = datetime.utcnow() + timedelta(days=1)
        need = g.recurring_need(quantity=40, unit="KG", recurrence_type="DAILY")
        occ = g.occurrence(need, occurrence_date=tomorrow, requested_quantity=40, unit="KG", status="MATCHED", quantity_matched=40)
        g.test_product = g.product_for(quantity_for_sale=50)
        g.allocation(occurrence=occ, producer=g.producer, product=g.test_product, quantity=40, unit_price=500, unit="KG")
    conn.close()
    return SimpleNamespace(
        dsn=pg_dsn, g=g, buyer_user=buyer_user, buyer_profile=buyer_profile,
        producer_user=producer_user, producer_profile=producer_profile, need=need, occ=occ,
    )


def _buyer_svc(session, m: SimpleNamespace) -> _Svc:
    svc = _Svc(session, m.buyer_user)
    svc._user_profile = m.buyer_profile
    return svc


def _producer_svc(session, m: SimpleNamespace) -> _Svc:
    svc = _Svc(session, m.producer_user)
    svc._user_profile = m.producer_profile
    return svc


def _accept(dsn, m: SimpleNamespace) -> str:
    """CAS 1 (moitié 1) : accepte la proposition — commande `RECURRING_SUPPLY` créée,
    `PENDING_PRODUCER_CONFIRMATION`. Réutilise `accept_match_proposal`, déjà éprouvé
    (commit 9c68dc3) — jamais recréé ici."""

    async def fn(session):
        return await _buyer_svc(session, m).accept_match_proposal(
            phone="+226", recurring_need_id=str(m.need), action="ACCEPT"
        )

    result = _run(dsn, fn)
    return result["order_ids"][0]


def _confirm_by_producer(dsn, m: SimpleNamespace, order_id: str) -> None:
    """CAS 1 (moitié 2) : le producteur confirme — `PENDING_PRODUCER_CONFIRMATION` ->
    `CONFIRMED`. Réutilise `confirm_order_by_producer` (`ProducerMgmtMixin`), déjà en place
    avant VS5 — vérifie que le mandat §3 (invariant ACCEPT ≠ commande définitivement exécutée)
    est bien respecté par le code déjà là, jamais réinventé."""

    async def fn(session):
        return await _producer_svc(session, m).confirm_order_by_producer(
            producer_phone="+226", order_id=order_id
        )

    _run(dsn, fn)


def _order(dsn, order_id: str) -> Order:
    async def fn(session):
        return await session.get(Order, uuid.UUID(order_id))

    return _run(dsn, fn)


def _history(dsn, order_id: str) -> list:
    async def fn(session):
        rows = (
            await session.execute(
                select(OrderStatusHistory)
                .where(OrderStatusHistory.order_id == uuid.UUID(order_id))
                .order_by(OrderStatusHistory.created_at)
            )
        ).scalars().all()
        return rows

    return _run(dsn, fn)


def _mark(dsn, m: SimpleNamespace, order_id: str, action: str) -> dict:
    async def fn(session):
        return await _producer_svc(session, m).mark_order_delivery_status(
            phone="+226", order_id=order_id, action=action
        )

    return _run(dsn, fn)


def _receive(dsn, m: SimpleNamespace, order_id: str, outcome: str, **kw) -> dict:
    async def fn(session):
        return await _buyer_svc(session, m).record_order_reception(
            phone="+226", order_id=order_id, outcome=outcome, **kw
        )

    return _run(dsn, fn)


def _accepted_and_confirmed(dsn, m: SimpleNamespace) -> str:
    order_id = _accept(dsn, m)
    _confirm_by_producer(dsn, m, order_id)
    return order_id


# ── CAS 1 : accepté -> confirmé producteur -> statut correct ──────────────

def test_cas1_producer_confirms_a_recurring_supply_order(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    order = _order(market.dsn, order_id)
    assert order.status == "CONFIRMED"
    assert order.order_type == "RECURRING_SUPPLY"
    assert str(order.delivery_status or "PENDING").upper() == "PENDING"


# ── CAS 2 : producteur signale le départ -> IN_TRANSIT ─────────────────────

def test_cas2_producer_marks_in_transit(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    result = _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    assert result["status"] == "success" and result["outcome"] == "UPDATED"
    order = _order(market.dsn, order_id)
    assert order.delivery_status == "IN_TRANSIT"
    history = _history(market.dsn, order_id)
    assert any(h.to_status == "IN_TRANSIT" and h.status_type == "DELIVERY" for h in history)


# ── CAS 3 : livraison signalée -> DELIVERED ────────────────────────────────

def test_cas3_producer_marks_delivered(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    result = _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    assert result["status"] == "success"
    order = _order(market.dsn, order_id)
    assert order.delivery_status == "DELIVERED"


# ── CAS 4 : buyer "tout est bon" -> réception enregistrée ─────────────────

def test_cas4_buyer_received_ok_closes_the_cycle(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    result = _receive(market.dsn, market, order_id, "RECEIVED")
    assert result["status"] == "success" and result["outcome"] == "RECORDED"
    order = _order(market.dsn, order_id)
    assert order.delivery_status == "RECEIVED"
    assert order.status == "COMPLETED"
    assert order.confirmed_at is not None


# ── CAS 5 : buyer signale une quantité fausse -> incident avec quantité réelle ─

def test_cas5_buyer_reports_wrong_quantity(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    result = _receive(
        market.dsn, market, order_id, "RECEIVED_WITH_ISSUE",
        issue_type="QUANTITY", received_quantity=32.0,
    )
    assert result["status"] == "success"
    order = _order(market.dsn, order_id)
    assert order.delivery_status == "RECEIVED_WITH_ISSUE"
    assert order.status == "CONFIRMED"  # jamais COMPLETED sur un incident — résolution humaine
    history = _history(market.dsn, order_id)
    incident = [h for h in history if h.to_status == "RECEIVED_WITH_ISSUE"][0]
    note = json.loads(incident.note)
    assert note["issue_type"] == "QUANTITY"
    assert note["received_quantity"] == 32.0


# ── CAS 6 : buyer signale un problème de qualité -> incident enregistré ────

def test_cas6_buyer_reports_quality_issue(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    result = _receive(
        market.dsn, market, order_id, "RECEIVED_WITH_ISSUE",
        issue_type="QUALITY", detail="Tomates abîmées",
    )
    assert result["status"] == "success"
    history = _history(market.dsn, order_id)
    incident = [h for h in history if h.to_status == "RECEIVED_WITH_ISSUE"][0]
    note = json.loads(incident.note)
    assert note["issue_type"] == "QUALITY"
    assert note["detail"] == "Tomates abîmées"


# ── CAS 7 : transition invalide -> refusée ─────────────────────────────────

def test_cas7_delivering_before_confirmation_leaves_state_untouched(market):
    """PENDING -> DELIVERED directement (sans passer par IN_TRANSIT) est refusé."""
    order_id = _accepted_and_confirmed(market.dsn, market)
    with pytest.raises(BusinessRuleException):
        _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    order = _order(market.dsn, order_id)
    assert str(order.delivery_status or "PENDING").upper() == "PENDING"


def test_cas7b_receiving_before_delivery_is_refused(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    with pytest.raises(BusinessRuleException):
        _receive(market.dsn, market, order_id, "RECEIVED")


# ── CAS 8 : double réception -> idempotent ─────────────────────────────────

def test_cas8_receiving_twice_is_idempotent(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    first = _receive(market.dsn, market, order_id, "RECEIVED")
    second = _receive(market.dsn, market, order_id, "RECEIVED")
    assert first["outcome"] == "RECORDED"
    assert second["outcome"] == "ALREADY_RECORDED"
    history = _history(market.dsn, order_id)
    assert len([h for h in history if h.to_status in ("RECEIVED", "RECEIVED_WITH_ISSUE")]) == 1


def test_cas8b_double_in_transit_is_idempotent(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    first = _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    second = _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    assert first["outcome"] == "UPDATED"
    assert second["outcome"] == "UNCHANGED"
    history = _history(market.dsn, order_id)
    assert len([h for h in history if h.to_status == "IN_TRANSIT"]) == 1


def test_cas8c_double_delivered_is_idempotent(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    first = _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    second = _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    assert first["outcome"] == "UPDATED"
    assert second["outcome"] == "UNCHANGED"


# ── CAS 9 : mauvais propriétaire -> refusé ─────────────────────────────────

def test_cas9_another_producer_cannot_mark_delivery(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    conn = psycopg2.connect(market.dsn)
    with conn, conn.cursor() as cur:
        intruder_user = insert(cur, "auth.users", phone=uniq("+226"))
        intruder_producer = insert(cur, "marketplace.producers", user_id=intruder_user)
    conn.close()
    intruder = SimpleNamespace(
        dsn=market.dsn, producer_user=SimpleNamespace(id=intruder_user),
        producer_profile=SimpleNamespace(id=intruder_producer),
    )

    async def fn(session):
        return await _producer_svc(session, intruder).mark_order_delivery_status(
            phone="+226", order_id=order_id, action="MARK_IN_TRANSIT"
        )

    with pytest.raises(BusinessRuleException):
        _run(market.dsn, fn)


def test_cas9b_another_buyer_cannot_record_reception(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    conn = psycopg2.connect(market.dsn)
    with conn, conn.cursor() as cur:
        intruder_user = insert(cur, "auth.users", phone=uniq("+226"))
        intruder_buyer = insert(cur, "marketplace.buyer_profiles", user_id=intruder_user)
    conn.close()
    intruder = SimpleNamespace(
        dsn=market.dsn, buyer_user=SimpleNamespace(id=intruder_user),
        buyer_profile=SimpleNamespace(id=intruder_buyer),
    )

    async def fn(session):
        return await _buyer_svc(session, intruder).record_order_reception(
            phone="+226", order_id=order_id, outcome="RECEIVED"
        )

    with pytest.raises(BusinessRuleException):
        _run(market.dsn, fn)


# ── CAS 10 : commande annulée -> ne peut être livrée/reçue ─────────────────

def test_cas10_a_cancelled_order_cannot_be_marked_in_transit(market):
    order_id = _accepted_and_confirmed(market.dsn, market)

    async def cancel(session):
        order = await session.get(Order, uuid.UUID(order_id))
        order.status = "CANCELLED"
        await session.flush()

    _run(market.dsn, cancel)

    with pytest.raises(BusinessRuleException):
        _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")


# ── CAS 11 : commande non livrée -> ne peut pas dire "tout est bon" ────────

def test_cas11_received_ok_before_delivered_is_refused(market):
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")  # IN_TRANSIT, pas encore DELIVERED
    with pytest.raises(BusinessRuleException):
        _receive(market.dsn, market, order_id, "RECEIVED")


# ── CAS 12 : le flux historique (non-recurring) reste inchangé ────────────

def test_cas12_mark_order_delivery_status_refuses_a_non_recurring_supply_order(market):
    """`mark_order_delivery_status` est scope STRICT `RECURRING_SUPPLY` — une commande
    préorder/RFQ classique (`confirm_delivery_and_payment`, chemin historique inchangé) ne doit
    JAMAIS pouvoir transiter par cette méthode."""
    conn = psycopg2.connect(market.dsn)
    with conn, conn.cursor() as cur:
        standard_order = insert(
            cur, "marketplace.orders", buyer_id=market.g.buyer, total_amount=500,
            order_type="STANDARD", status="CONFIRMED", payment_status="PENDING",
        )
    conn.close()

    with pytest.raises(BusinessRuleException):
        _mark(market.dsn, market, str(standard_order), "MARK_IN_TRANSIT")


# ── Intégration bout-en-bout (mandat §15) ──────────────────────────────────

def test_e2e_full_happy_path_besoin_to_reception(market):
    """40kg tomate -> occurrence -> matching (déjà posé par la fixture `market`) -> ACCEPT ->
    Order créée -> confirmation producteur -> IN_TRANSIT -> DELIVERED -> "Tout est bon" -> état
    final vérifié, stock vérifié, historique vérifié."""
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    _receive(market.dsn, market, order_id, "RECEIVED")

    order = _order(market.dsn, order_id)
    assert order.status == "COMPLETED"
    assert order.delivery_status == "RECEIVED"

    async def stock(session):
        from sqlalchemy import text as _text
        row = (
            await session.execute(
                _text("select quantity_for_sale from marketplace.products where id = :pid"),
                {"pid": str(market.g.test_product)},
            )
        ).first()
        return float(row[0])

    assert _run(market.dsn, stock) == 10.0  # 50 - 40, débité une seule fois, à l'ACCEPT

    history = _history(market.dsn, order_id)
    transitions = [(h.status_type, h.from_status, h.to_status) for h in history]
    assert ("DELIVERY", "PENDING", "IN_TRANSIT") in transitions
    assert ("DELIVERY", "IN_TRANSIT", "DELIVERED") in transitions
    assert ("DELIVERY", "DELIVERED", "RECEIVED") in transitions


def test_e2e_reception_with_incorrect_quantity(market):
    """Même flux complet, mais la réception signale une quantité reçue différente de la quantité
    commandée — l'incident est tracé, `Order.status` ne bascule jamais `COMPLETED`."""
    order_id = _accepted_and_confirmed(market.dsn, market)
    _mark(market.dsn, market, order_id, "MARK_IN_TRANSIT")
    _mark(market.dsn, market, order_id, "MARK_DELIVERED")
    _receive(market.dsn, market, order_id, "RECEIVED_WITH_ISSUE", issue_type="QUANTITY", received_quantity=35.0)

    order = _order(market.dsn, order_id)
    assert order.delivery_status == "RECEIVED_WITH_ISSUE"
    assert order.status == "CONFIRMED"
    assert order.confirmed_at is None

    history = _history(market.dsn, order_id)
    incident = [h for h in history if h.to_status == "RECEIVED_WITH_ISSUE"][0]
    note = json.loads(incident.note)
    assert note == {"issue_type": "QUANTITY", "detail": None, "received_quantity": 35.0}
