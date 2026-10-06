"""Annulation producteur d'une commande CONFIRMÉE — décision produit #1
(Phase 5, 2026-09-04).

## Le gap fermé

`CONFIRMED` était le SEUL état du produit sans sortie pour le producteur :
ne pouvant ni livrer (rupture, aléa) ni se rétracter, il devait demander à
l'acheteur d'annuler — un contournement hors-app, ou une commande figée
pour toujours.

La politique implémentée est la **symétrie stricte** du chemin acheteur
(`BuyerMixin.cancel_pending_order`) : mêmes statuts, même recrédit de
stock, même colonne `cancellation_role`, même motif libre, aucun
remboursement (paiement à la livraison). Ce que le dépôt ne déterminait
PAS — limite anti-abus producteur, réouverture de l'enchère — n'a
volontairement pas été inventé (voir
`docs/PRODUCT_DECISION_REGISTER_2026-09-04.md`)."""
from __future__ import annotations

import types
import uuid
from decimal import Decimal

import pytest

from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.producer import ProducerMgmtMixin
from tests.conftest import run


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


def _order(status="CONFIRMED", items=None, winning_bid_id=None, payment_status="PENDING"):
    return types.SimpleNamespace(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), status=status,
        payment_status=payment_status, delivery_status="PENDING",
        items=items or [], winning_bid_id=winning_bid_id,
        cancellation_role=None, delivery_desc=None, confirmed_at=None,
        total_amount=2500.0, currency="XOF",
    )


class _Rows(list):
    """Liste de tuples-ligne qui supporte AUSSI `.first()`/`.all()` — voir la
    même classe dans test_cancel_pending_order_confirmed_gap.py pour le
    détail (protocole d'itération vs `types.SimpleNamespace`)."""

    def first(self):
        return self[0] if self else None

    def all(self):
        return list(self)


class _FakeSession:
    """Dispatch par contenu SQL compilé — aucun ordre d'appel supposé
    (même convention que le reste de cette suite)."""

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

    async def scalars(self, stmt):
        # (2026-09-21, audit latence — N+1) : `cancel_confirmed_order`
        # verrouille désormais TOUS les produits de la commande en UNE
        # requête `IN (...)` plutôt qu'une par item — même correspondance
        # par sous-chaîne que `.scalar()` ci-dessus, mais collecte TOUS les
        # matches au lieu du premier.
        sql = self._sql(stmt)
        matched = [
            item.product
            for item in self._order.items
            if item.product and str(item.product_id) in sql
        ]
        return _Rows(matched)

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


class TestProducerCanCancelAConfirmedOrder:
    def test_preorder_order_is_cancelled_stock_recredited_buyer_notified(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id, qty=100.0)
        order = _order(items=[_item(product, qty=10.0)])
        session = _FakeSession(order)
        svc = _service(session, types.SimpleNamespace(id=producer_id))

        result = run(
            svc.cancel_confirmed_order(
                producer_phone="+22670000001",
                order_id=str(order.id),
                reason="rupture de stock",
            )
        )

        assert result["outcome"] == "CANCELLED"
        assert order.status == "CANCELLED"
        assert order.cancellation_role == "PRODUCER"
        assert product.quantity_for_sale == 110.0  # recrédité
        assert "[CancelReason] rupture de stock" in (order.delivery_desc or "")
        assert len(session.outbox_inserts) == 1  # acheteur notifié
        history = [o for o in session.added if type(o).__name__ == "OrderStatusHistory"]
        assert len(history) == 1
        assert (history[0].from_status, history[0].to_status) == ("CONFIRMED", "CANCELLED")

    def test_rfq_order_is_cancelled_without_touching_stock(self):
        """Commande issue d'une enchère : aucun `OrderItem`, donc aucun
        stock catalogue à restituer — la propriété est établie via
        `winning_bid_id`."""
        order = _order(items=[], winning_bid_id=uuid.uuid4())
        session = _FakeSession(order, owns_via_items=False, owns_via_bid=True)
        svc = _service(session)

        result = run(
            svc.cancel_confirmed_order(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert result["outcome"] == "CANCELLED"
        assert order.status == "CANCELLED"
        assert len(session.outbox_inserts) == 1

    def test_no_buyer_phone_never_blocks_the_cancellation(self):
        order = _order(items=[])
        session = _FakeSession(order, buyer_phone=None)
        svc = _service(session)

        result = run(
            svc.cancel_confirmed_order(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert result["outcome"] == "CANCELLED"
        assert not session.outbox_inserts


class TestStockRecreditSurvivesRealDecimalColumns:
    """Incident réel (2026-09-15) : `Product.quantity_for_sale` est une
    colonne `Numeric` — chargée en `decimal.Decimal` sur une VRAIE ligne
    SQLAlchemy, jamais un `float` nu. `resolve_stock_debit` renvoie
    toujours un `float`. `Decimal += float` lève `TypeError` — masqué côté
    agent par `SafeDatabaseError` en "erreur technique", aucune commande
    n'était en réalité jamais annulée. Les autres tests de ce fichier
    utilisent `quantity_for_sale=float` (via `_product` ci-dessus) — un
    `types.SimpleNamespace` ne reproduit pas le type RÉEL d'une colonne
    Numeric, ce qui a laissé ce bug totalement invisible aux tests
    existants malgré une suite déjà large. Celui-ci reproduit le type
    exact vu en production."""

    def test_cancellation_recredits_stock_when_the_column_is_a_real_decimal(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id, qty=Decimal("100.0"))
        order = _order(items=[_item(product, qty=10.0)])
        session = _FakeSession(order)
        svc = _service(session, types.SimpleNamespace(id=producer_id))

        result = run(
            svc.cancel_confirmed_order(
                producer_phone="+22670000001",
                order_id=str(order.id),
                reason="rupture de stock",
            )
        )

        assert result["outcome"] == "CANCELLED"
        assert product.quantity_for_sale == 110.0


class TestProducerCanDeclineBeforeConfirming:
    """(2026-09-13, confirmation explicite producteur) : refuser une
    commande AVANT de la confirmer est désormais un cas d'usage légitime,
    au même titre qu'annuler après confirmation — mécanique strictement
    identique, seul `from_status`/la note d'historique diffèrent."""

    def test_pending_confirmation_order_is_declined_stock_recredited_buyer_notified(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id, qty=100.0)
        order = _order(
            status="PENDING_PRODUCER_CONFIRMATION", items=[_item(product, qty=10.0)]
        )
        session = _FakeSession(order)
        svc = _service(session, types.SimpleNamespace(id=producer_id))

        result = run(
            svc.cancel_confirmed_order(
                producer_phone="+22670000001",
                order_id=str(order.id),
                reason="rupture de stock",
            )
        )

        assert result["outcome"] == "CANCELLED"
        assert order.status == "CANCELLED"
        assert product.quantity_for_sale == 110.0  # recrédité
        assert len(session.outbox_inserts) == 1  # acheteur notifié
        history = [o for o in session.added if type(o).__name__ == "OrderStatusHistory"]
        assert len(history) == 1
        assert (history[0].from_status, history[0].to_status) == (
            "PENDING_PRODUCER_CONFIRMATION",
            "CANCELLED",
        )
        assert history[0].note == "declined_by_producer"


class TestGuards:
    def test_a_producer_who_does_not_own_the_order_is_rejected(self):
        order = _order(items=[])
        session = _FakeSession(order, owns_via_items=False, owns_via_bid=False)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(
                svc.cancel_confirmed_order(
                    producer_phone="+22670000009", order_id=str(order.id)
                )
            )
        assert exc.value.reason == "not_owner"
        assert order.status == "CONFIRMED"
        assert not session.outbox_inserts

    @pytest.mark.parametrize("status", ["COMPLETED", "DRAFT", "SUPERSEDED"])
    def test_non_confirmed_orders_are_rejected(self, status):
        order = _order(status=status, items=[])
        session = _FakeSession(order)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(
                svc.cancel_confirmed_order(
                    producer_phone="+22670000001", order_id=str(order.id)
                )
            )
        assert exc.value.reason == "order_not_confirmed"
        assert order.status == status
        assert not session.outbox_inserts

    def test_invalid_order_id_is_a_clean_business_error(self):
        session = _FakeSession(_order())
        svc = _service(session)
        with pytest.raises(BusinessRuleException) as exc:
            run(svc.cancel_confirmed_order(producer_phone="+2267", order_id="pas-un-uuid"))
        assert exc.value.reason == "invalid_order_id"


class TestIdempotenceAndConcurrency:
    def test_second_cancellation_is_idempotent_and_never_re_credits_stock(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id, qty=100.0)
        order = _order(items=[_item(product, qty=10.0)])
        producer = types.SimpleNamespace(id=producer_id)

        session1 = _FakeSession(order)
        run(
            _service(session1, producer).cancel_confirmed_order(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert product.quantity_for_sale == 110.0
        assert len(session1.outbox_inserts) == 1

        # 2e tentative sur le MÊME objet métier (déjà muté), session fraîche.
        session2 = _FakeSession(order)
        result = run(
            _service(session2, producer).cancel_confirmed_order(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert result["outcome"] == "ALREADY_CANCELLED"
        assert product.quantity_for_sale == 110.0  # jamais un 2e recrédit
        assert not session2.outbox_inserts  # jamais une 2e notification

    def test_delivery_closure_becomes_impossible_after_cancellation(self):
        """Exigence explicite du mandat : après annulation, la clôture
        livraison/paiement (F1) ne doit plus jamais aboutir. Les deux
        chemins verrouillent la même ligne et exigent `CONFIRMED` — le
        premier à committer gagne."""
        producer_id = uuid.uuid4()
        product = _product(producer_id)
        order = _order(items=[_item(product)])
        producer = types.SimpleNamespace(id=producer_id)

        run(
            _service(_FakeSession(order), producer).cancel_confirmed_order(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert order.status == "CANCELLED"

        with pytest.raises(BusinessRuleException) as exc:
            run(
                _service(_FakeSession(order), producer).confirm_delivery_and_payment(
                    producer_phone="+22670000001", order_id=str(order.id)
                )
            )
        assert exc.value.reason == "order_not_confirmed"
        assert order.status == "CANCELLED"
        assert order.payment_status == "PENDING"  # jamais passée à PAID

    def test_row_lock_is_taken_on_the_order(self):
        """Preuve sur le SQL compilé : la première lecture verrouille la
        ligne, comme `confirm_delivery_and_payment` — sans quoi les deux
        chemins pourraient s'entrelacer."""
        import inspect

        source = inspect.getsource(ProducerMgmtMixin.cancel_confirmed_order)
        assert ".with_for_update()" in source
        assert source.count(".with_for_update()") >= 2  # Order + Product


class TestNoRefundLogicWasInvented:
    def test_payment_status_is_never_touched(self):
        """Paiement à la livraison : rien n'a été encaissé, il n'y a donc
        rien à rembourser — et surtout aucun statut de paiement à inventer."""
        order = _order(items=[])
        svc = _service(_FakeSession(order))
        run(
            svc.cancel_confirmed_order(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert order.payment_status == "PENDING"
        assert order.delivery_status == "PENDING"

    def test_no_producer_abuse_counter_was_added(self):
        """Décision produit explicitement NON prise : aucune sanction
        producteur n'a été inventée (le compteur `MAX_CANCELLATIONS`
        existant reste spécifique à l'acheteur)."""
        import inspect

        raw = inspect.getsource(ProducerMgmtMixin.cancel_confirmed_order)
        # La docstring CITE volontairement la règle écartée — seul le CODE
        # réel est audité ici (même précaution que les autres tests
        # structurels de cette suite).
        body = raw.split('"""')[2] if raw.count('"""') >= 2 else raw
        source = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        assert "MAX_CANCELLATIONS" not in source
        assert "_enforce_cancellation_limit" not in source
        assert "account_status" not in source
