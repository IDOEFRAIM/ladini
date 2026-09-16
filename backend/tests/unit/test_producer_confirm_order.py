"""Confirmation explicite producteur d'une commande reçue (2026-09-13).

## Le gap fermé

Une précommande directe (paiement à la livraison,
`BuyerMixin.confirm_preorder_draft`) passait `CONFIRMED` sans AUCUNE étape
d'acceptation par le producteur — seulement une notification informative
("préparez la commande"), signalé comme un gap réel par un producteur
utilisant réellement le produit. `confirm_order_by_producer` ferme ce gap :
la commande entre désormais en `PENDING_PRODUCER_CONFIRMATION` à sa création
et ne devient `CONFIRMED` qu'après ce geste explicite.

Même gabarit mécanique que `cancel_confirmed_order`
(`test_producer_cancel_confirmed_order.py`) : propriété résolue sur les deux
origines réelles d'une commande, verrou `FOR UPDATE`, notification acheteur
en Outbox, même transaction — rien n'est réinventé."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.producer import ProducerMgmtMixin


async def _async_return(value):
    return value


def _product(producer_id, qty=100.0):
    return types.SimpleNamespace(
        id=uuid.uuid4(), name="Riz", quantity_for_sale=qty, unit="KG",
        producer_id=producer_id,
    )


def _item(product, qty=10.0):
    return types.SimpleNamespace(
        product_id=product.id, quantity=qty, price_at_sale=250.0,
        base_unit_quantity=None, product=product,
    )


def _order(status="PENDING_PRODUCER_CONFIRMATION", items=None, winning_bid_id=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), status=status,
        payment_status="PENDING", delivery_status="PENDING",
        items=items or [], winning_bid_id=winning_bid_id,
        cancellation_role=None, delivery_desc=None, confirmed_at=None,
        total_amount=2500.0, currency="XOF",
    )


class _FakeSession:
    """Dispatch par contenu SQL compilé — même convention que
    `test_producer_cancel_confirmed_order.py`."""

    def __init__(self, order, *, owns_via_items=True, owns_via_bid=False,
                 buyer_phone="+22670000099"):
        self._order = order
        self._owns_via_items = owns_via_items
        self._owns_via_bid = owns_via_bid
        self._buyer_phone = buyer_phone
        self._order_served = False
        self.added: list = []
        self.outbox_inserts: list = []

    def _sql(self, stmt):
        from sqlalchemy.dialects import postgresql

        return str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )

    async def scalar(self, stmt):
        sql = self._sql(stmt)
        if "marketplace.order_items" in sql and "marketplace.products" in sql:
            return uuid.uuid4() if self._owns_via_items else None
        if "marketplace.bids" in sql:
            return uuid.uuid4() if self._owns_via_bid else None
        if "marketplace.products" in sql:
            for item in self._order.items:
                if str(item.product_id) in sql:
                    return item.product
            return None
        if "marketplace.orders" in sql and not self._order_served:
            self._order_served = True
            return self._order
        return None

    async def execute(self, stmt):
        from sqlalchemy.sql.dml import Insert as _InsertStmt

        if isinstance(stmt, _InsertStmt):
            self.outbox_inserts.append(stmt)
            return types.SimpleNamespace(all=lambda: [1])
        phone = self._buyer_phone
        return types.SimpleNamespace(first=lambda: ((phone,) if phone else None))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _service(session, producer=None):
    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    producer = producer or types.SimpleNamespace(id=uuid.uuid4())
    user = types.SimpleNamespace(id=uuid.uuid4())
    svc.get_producer_profile = lambda phone=None: _async_return((user, producer))
    return svc


class TestProducerCanConfirmAPendingOrder:
    def test_preorder_order_is_confirmed_and_buyer_notified(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id, qty=100.0)
        order = _order(items=[_item(product, qty=10.0)])
        session = _FakeSession(order)
        svc = _service(session, types.SimpleNamespace(id=producer_id))

        result = run(
            svc.confirm_order_by_producer(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )

        assert result["outcome"] == "CONFIRMED"
        assert order.status == "CONFIRMED"
        # La confirmation ne touche jamais le stock — il a déjà été débité
        # à la création de la commande (BuyerMixin.confirm_preorder_draft).
        assert product.quantity_for_sale == 100.0
        assert len(session.outbox_inserts) == 1  # acheteur notifié
        history = [o for o in session.added if type(o).__name__ == "OrderStatusHistory"]
        assert len(history) == 1
        assert (history[0].from_status, history[0].to_status) == (
            "PENDING_PRODUCER_CONFIRMATION",
            "CONFIRMED",
        )
        assert history[0].note == "confirmed_by_producer"

    def test_rfq_order_is_confirmed_without_touching_stock(self):
        order = _order(items=[], winning_bid_id=uuid.uuid4())
        session = _FakeSession(order, owns_via_items=False, owns_via_bid=True)
        svc = _service(session)

        result = run(
            svc.confirm_order_by_producer(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert result["outcome"] == "CONFIRMED"
        assert order.status == "CONFIRMED"
        assert len(session.outbox_inserts) == 1

    def test_no_buyer_phone_never_blocks_the_confirmation(self):
        order = _order(items=[])
        session = _FakeSession(order, buyer_phone=None)
        svc = _service(session)

        result = run(
            svc.confirm_order_by_producer(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert result["outcome"] == "CONFIRMED"
        assert not session.outbox_inserts


class TestGuards:
    def test_a_producer_who_does_not_own_the_order_is_rejected(self):
        order = _order(items=[])
        session = _FakeSession(order, owns_via_items=False, owns_via_bid=False)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(
                svc.confirm_order_by_producer(
                    producer_phone="+22670000009", order_id=str(order.id)
                )
            )
        assert exc.value.reason == "not_owner"
        assert order.status == "PENDING_PRODUCER_CONFIRMATION"
        assert not session.outbox_inserts

    @pytest.mark.parametrize(
        "status", ["COMPLETED", "DRAFT", "SUPERSEDED", "CANCELLED", "CONFIRMED"]
    )
    def test_orders_not_pending_confirmation_are_rejected(self, status):
        """Une commande déjà `CONFIRMED` n'a plus de raison d'être
        reconfirmée — voir `test_a_second_confirmation_is_idempotent` pour
        le cas particulier `ALREADY_CONFIRMED`, qui ne lève pas."""
        if status == "CONFIRMED":
            pytest.skip("cas particulier idempotent, testé séparément")
        order = _order(status=status, items=[])
        session = _FakeSession(order)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(
                svc.confirm_order_by_producer(
                    producer_phone="+22670000001", order_id=str(order.id)
                )
            )
        assert exc.value.reason == "order_not_pending_confirmation"
        assert order.status == status
        assert not session.outbox_inserts

    def test_invalid_order_id_is_a_clean_business_error(self):
        session = _FakeSession(_order())
        svc = _service(session)
        with pytest.raises(BusinessRuleException) as exc:
            run(svc.confirm_order_by_producer(producer_phone="+2267", order_id="pas-un-uuid"))
        assert exc.value.reason == "invalid_order_id"


class TestIdempotenceAndConcurrency:
    def test_a_second_confirmation_is_idempotent(self):
        producer_id = uuid.uuid4()
        order = _order(status="CONFIRMED", items=[])
        producer = types.SimpleNamespace(id=producer_id)
        session = _FakeSession(order)

        result = run(
            _service(session, producer).confirm_order_by_producer(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert result["outcome"] == "ALREADY_CONFIRMED"
        assert order.status == "CONFIRMED"
        assert not session.outbox_inserts  # jamais une 2e notification

    def test_row_lock_is_taken_on_the_order(self):
        import inspect

        source = inspect.getsource(ProducerMgmtMixin.confirm_order_by_producer)
        assert ".with_for_update()" in source
