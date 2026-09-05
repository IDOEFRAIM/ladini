"""`BuyerMixin.cancel_pending_order` — fermeture du gap
`BUYER_CANCEL_ORDER` structurellement inatteignable (audit produit
post-F1-F4, 2026-09-04).

## Le gap réel fermé par ce fichier

`BUYER_CANCEL_ORDER` est un goal déclaré, tunnelé (`order_tracking`),
avec un handler réel (`cancel_order` -> `gw.cancel_pending_order(...)`) —
mais la méthode DB ne gardait QUE `order.status == "PENDING"`. Recherche
exhaustive sur TOUTE cette session : aucun chemin de création de commande
vivant ne pose jamais ce statut (`"PENDING"` n'est que le défaut de
colonne SQLAlchemy, toujours écrasé — `create_preorder_draft` pose
`"DRAFT"`, `select_winning_bid` pose `"CONFIRMED"` directement). Le
handler était donc une "fonctionnalité annoncée qui existe seulement en
apparence" — exactement le type de gap que cet audit devait débusquer.

Correctif : le garde couvre désormais aussi `"CONFIRMED"` (le statut RÉEL
d'une commande pas encore clôturée), avec verrouillage FOR UPDATE déjà en
place qui la sérialise proprement contre
`ProducerMgmtMixin.confirm_delivery_and_payment` (F1, même statut exigé).
Le producteur est notifié (nouveau template
`ORDER_CANCELLED_BY_BUYER_PRODUCER`) uniquement quand une commande déjà
`CONFIRMED` est annulée — jamais pour un `"PENDING"` legacy (aucun
producteur n'a jamais "vu" une telle commande)."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from agriconnect.services.database.buyer import BuyerMixin
from agriconnect.services.database.errors import BusinessRuleException


async def _async_return(value):
    return value


def _fake_buyer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4(), account_status="ACTIVE")
    profile = types.SimpleNamespace(id=uuid.uuid4())
    return user, profile


def _product(producer_id, qty=100.0):
    return types.SimpleNamespace(
        id=uuid.uuid4(), name="Riz", quantity_for_sale=qty, unit="KG",
        producer_id=producer_id,
    )


def _item(product, price=250.0, qty=10.0):
    return types.SimpleNamespace(
        product_id=product.id, quantity=qty, price_at_sale=price,
        base_unit_quantity=None, product=product,
    )


def _order(status, items=None, winning_bid_id=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), status=status,
        items=items or [], winning_bid_id=winning_bid_id,
        cancellation_role=None, delivery_desc=None,
    )


class _FakeSession:
    """Dispatch `.scalar()`/`.execute()` par contenu SQL compilé — même
    convention que le reste de cette suite (aucun ordre d'appel supposé)."""

    def __init__(self, order, phones_by_producer=None):
        self._order = order
        self._phones_by_producer = phones_by_producer or {}
        self._order_served = False
        self.outbox_inserts: list = []

    def _sql(self, stmt):
        from sqlalchemy.dialects import postgresql

        return str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )

    async def scalar(self, stmt):
        sql = self._sql(stmt)
        if "count(" in sql.lower():
            return 0  # `_enforce_cancellation_limit` : jamais bloqué dans ces tests
        if "marketplace.orders" in sql and not self._order_served:
            self._order_served = True
            return self._order
        if "marketplace.products" in sql:
            for item in self._order.items:
                if str(item.product_id) in sql:
                    return item.product
            return None
        return None

    async def execute(self, stmt):
        from sqlalchemy.sql.dml import Insert as _InsertStmt

        if isinstance(stmt, _InsertStmt):
            self.outbox_inserts.append(stmt)
            return types.SimpleNamespace(all=lambda: [1])
        sql = self._sql(stmt)
        for producer_id, phone in self._phones_by_producer.items():
            if str(producer_id) in sql:
                return types.SimpleNamespace(first=lambda: (phone,))
        # Résolution via `Bid.id == order.winning_bid_id` : aucun producer_id
        # littéral dans le SQL compilé (filtré par bid_id, joint sur
        # `Bid.producer_id`) — matché sur la présence de la table `bids`.
        if "marketplace.bids" in sql and self._phones_by_producer:
            phone = next(iter(self._phones_by_producer.values()))
            return types.SimpleNamespace(first=lambda: (phone,))
        return types.SimpleNamespace(first=lambda: None)

    def add(self, obj):
        pass

    async def flush(self):
        pass


def _service(session):
    class _Svc(BuyerMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc.get_buyer_profile = lambda phone: _async_return(_fake_buyer_profile())
    return svc


class TestConfirmedOrderIsNowCancellable:
    """Le coeur du gap : avant ce correctif, CE scénario levait TOUJOURS
    `BusinessRuleException(reason="not_pending")` — pour toute commande
    qu'un acheteur réel voudrait annuler."""

    def test_cancel_confirmed_preorder_order_succeeds_and_recredits_stock(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id, qty=100.0)
        item = _item(product, qty=10.0)
        order = _order("CONFIRMED", items=[item])
        session = _FakeSession(order, {producer_id: "+22670000001"})
        svc = _service(session)

        result = run(svc.cancel_pending_order(order_id=str(order.id), phone="+22670000099"))

        assert result["status"] == "success"
        assert order.status == "CANCELLED"
        assert order.cancellation_role == "BUYER"
        assert product.quantity_for_sale == 110.0  # recrédité
        assert len(session.outbox_inserts) == 1  # producteur notifié
        assert "restitués" in result["message"]

    def test_cancel_confirmed_rfq_order_notifies_producer_via_bid_and_skips_stock_note(self):
        """Commande issue d'une enchère : aucun `OrderItem`, donc aucun
        stock à restituer (correctement, un no-op) — le producteur est
        néanmoins notifié via la résolution `winning_bid_id` -> `Bid.producer_id`."""
        producer_id = uuid.uuid4()
        order = _order("CONFIRMED", items=[], winning_bid_id=uuid.uuid4())
        session = _FakeSession(order, {producer_id: "+22670000002"})
        svc = _service(session)

        result = run(svc.cancel_pending_order(order_id=str(order.id), phone="+22670000099"))

        assert result["status"] == "success"
        assert order.status == "CANCELLED"
        assert len(session.outbox_inserts) == 1
        assert "restitués" not in result["message"]

    def test_no_producer_phone_resolved_never_blocks_cancellation(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id)
        order = _order("CONFIRMED", items=[_item(product)])
        session = _FakeSession(order, {})  # aucun téléphone résolu
        svc = _service(session)

        result = run(svc.cancel_pending_order(order_id=str(order.id), phone="+22670000099"))
        assert result["status"] == "success"
        assert not session.outbox_inserts


class TestLegacyPendingStatusStillWorksNoProducerToNotify:
    def test_cancel_pending_order_succeeds_without_notification(self):
        """Défensif : si un jour un chemin recommence à poser `"PENDING"`,
        l'annulation continue de fonctionner — mais aucun producteur n'a
        jamais "vu" une commande PENDING, donc aucune notification."""
        producer_id = uuid.uuid4()
        product = _product(producer_id)
        order = _order("PENDING", items=[_item(product)])
        session = _FakeSession(order, {producer_id: "+22670000001"})
        svc = _service(session)

        result = run(svc.cancel_pending_order(order_id=str(order.id), phone="+22670000099"))
        assert result["status"] == "success"
        assert not session.outbox_inserts


class TestNonCancellableStatusesStillRejected:
    @pytest.mark.parametrize("status", ["DRAFT", "COMPLETED", "CANCELLED", "SUPERSEDED"])
    def test_status_is_rejected(self, status):
        order = _order(status)
        session = _FakeSession(order)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(svc.cancel_pending_order(order_id=str(order.id), phone="+22670000099"))
        assert exc.value.reason == "not_cancellable"
        assert not session.outbox_inserts

    def test_retry_after_cancellation_is_rejected_never_double_notifies(self):
        """Idempotence naturelle (même motif que F2/F3) : la 2e tentative
        échoue avant d'atteindre le code de notification, `order.status`
        étant déjà `"CANCELLED"` par la 1re — pas de primitive nouvelle."""
        producer_id = uuid.uuid4()
        product = _product(producer_id)
        order = _order("CONFIRMED", items=[_item(product)])
        session = _FakeSession(order, {producer_id: "+22670000001"})
        svc = _service(session)

        run(svc.cancel_pending_order(order_id=str(order.id), phone="+22670000099"))
        assert len(session.outbox_inserts) == 1

        session2 = _FakeSession(order, {producer_id: "+22670000001"})
        svc2 = _service(session2)
        with pytest.raises(BusinessRuleException) as exc:
            run(svc2.cancel_pending_order(order_id=str(order.id), phone="+22670000099"))
        assert exc.value.reason == "not_cancellable"
        assert not session2.outbox_inserts
