"""Parcours complets — clôture F1 (paiement à la livraison), 2026-09-04.

Mandat §25/§26 : les DEUX parcours business réels doivent fonctionner de
bout en bout, en réutilisant les VRAIS services, jamais des fonctions
isolées :

```
RFQ      : create_auction → place_bid → select_winning_bid → confirm_delivery_and_payment → COMPLETED
PREORDER : (checkout déjà audité séparément) → confirm_delivery_and_payment → COMPLETED
```

Chaque étape RÉUTILISE les MÊMES objets Python d'une étape à l'autre — les
mutations d'une étape sont donc RÉELLEMENT visibles par la suivante, même
convention que `test_auction_bid_full_lifecycle_e2e.py` (dont ce fichier
reprend directement le gabarit pour le tronçon RFQ)."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.auction import AuctionMixin
from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.errors import BusinessRuleException


async def _async_return(value):
    return value


def _auction(**overrides):
    base = dict(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), quantity=10.0, unit="TONNE",
        target_zone_id=None, sub_category_id=uuid.uuid4(), status="OPEN", winner_bid_id=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakePlaceBidSession:
    def __init__(self, auction, existing_bid):
        self._auction = auction
        self._existing_bid = existing_bid
        self._n = 0
        self.added: list = []

    async def scalar(self, _stmt):
        self._n += 1
        return self._auction if self._n == 1 else self._existing_bid

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _FakeSelectWinningBidSession:
    def __init__(self, row, owner_phone=None):
        from sqlalchemy.sql.dml import Update as _UpdateStmt

        self._row = row
        self._UpdateStmt = _UpdateStmt
        self._owner_phone = owner_phone
        self.added: list = []
        self._select_served = False

    async def scalar(self, stmt):
        return self._owner_phone

    async def execute(self, stmt):
        if isinstance(stmt, self._UpdateStmt):
            return types.SimpleNamespace(rowcount=0, scalars=lambda: types.SimpleNamespace(all=lambda: []))
        if not self._select_served:
            self._select_served = True
            row = self._row
            return types.SimpleNamespace(fetchone=lambda: row)
        return types.SimpleNamespace(all=lambda: [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _FakeConfirmDeliveryPaymentSession:
    """Combine ce dont `confirm_delivery_and_payment` a besoin : 1er
    `.scalar()` -> Order (FOR UPDATE), 2e -> appartenance OrderItem, 3e ->
    appartenance Bid ; `.execute()` -> téléphone acheteur puis outbox."""

    def __init__(self, order, *, owns_via_items=False, owns_via_bid=False, buyer_phone="+22670000099"):
        self._order = order
        self._owns_via_items = owns_via_items
        self._owns_via_bid = owns_via_bid
        self._buyer_phone = buyer_phone
        self._n = 0
        self.added: list = []

    async def scalar(self, _stmt):
        self._n += 1
        if self._n == 1:
            return self._order
        if self._n == 2:
            return uuid.uuid4() if self._owns_via_items else None
        if self._n == 3:
            return uuid.uuid4() if self._owns_via_bid else None
        return None

    async def execute(self, stmt):
        from sqlalchemy.sql.dml import Insert as _InsertStmt

        if isinstance(stmt, _InsertStmt):
            return types.SimpleNamespace(all=lambda: [1])
        return types.SimpleNamespace(first=lambda: (self._buyer_phone,))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _auction_svc(session, *, producer=None):
    class _Svc(AuctionMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    if producer is not None:
        user = types.SimpleNamespace(id=uuid.uuid4())
        svc.get_producer_profile = lambda phone=None: _async_return((user, producer))
    return svc


def _producer_svc(session, producer):
    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    user = types.SimpleNamespace(id=uuid.uuid4())
    svc.get_producer_profile = lambda phone: _async_return((user, producer))
    return svc


class TestRfqFullJourneyToCompletion:
    def test_request_bids_winner_delivery_payment_completed(self):
        buyer_phone = "+22670000099"
        phone_a = "+22670000001"
        producer_a = types.SimpleNamespace(id=uuid.uuid4())
        auction = _auction()

        # ── PRODUCER A -> bid 250 ──
        svc_a = _auction_svc(_FakePlaceBidSession(auction, None), producer=producer_a)
        session_a = svc_a.session
        run(svc_a.place_bid(auction_id=str(auction.id), phone=phone_a, offered_price=250.0))
        bid_a = session_a.added[0]

        # ── BUYER -> select A (winner) ──
        sub_cat = types.SimpleNamespace(name="Riz")
        row = (bid_a, auction, "Producteur A", phone_a, sub_cat)
        svc_buyer = _auction_svc(_FakeSelectWinningBidSession(row, owner_phone=buyer_phone))
        select_session = svc_buyer.session
        win = run(svc_buyer.select_winning_bid(bid_id=str(bid_a.id), phone=buyer_phone))
        assert win["status"] == "success"
        order = next(o for o in select_session.added if type(o).__name__ == "Order")
        assert order.status == "CONFIRMED"
        # `select_winning_bid` ne pose pas `payment_status`/`delivery_status`
        # explicitement — un VRAI flush Postgres appliquerait les défauts de
        # colonne (`PENDING`/`PENDING`) avant l'INSERT ; ce faux moteur (même
        # limite honnête que le reste de cette suite) ne simule pas ce
        # mécanisme ORM. On le reproduit explicitement ici pour représenter
        # fidèlement l'état RÉEL post-flush, sans re-tester SQLAlchemy lui-même.
        order.payment_status = order.payment_status or "PENDING"
        order.delivery_status = order.delivery_status or "PENDING"

        # ── PRODUCER A -> confirme livraison + paiement à la livraison ──
        confirm_session = _FakeConfirmDeliveryPaymentSession(
            order, owns_via_items=False, owns_via_bid=True, buyer_phone=buyer_phone
        )
        svc_confirm = _producer_svc(confirm_session, producer_a)
        closure = run(
            svc_confirm.confirm_delivery_and_payment(
                producer_phone=phone_a, order_id=str(order.id)
            )
        )

        assert closure["status"] == "success"
        assert closure["outcome"] == "COMPLETED"
        assert order.status == "COMPLETED"
        assert order.payment_status == "PAID"
        assert order.delivery_status == "DELIVERED"
        history = [o for o in confirm_session.added if type(o).__name__ == "OrderStatusHistory"]
        assert len(history) == 3

        # Un producteur B (non concerné) ne doit jamais pouvoir rejouer
        # cette clôture sur la même commande.
        producer_b = types.SimpleNamespace(id=uuid.uuid4())
        session_b = _FakeConfirmDeliveryPaymentSession(order, owns_via_items=False, owns_via_bid=False)
        svc_b = _producer_svc(session_b, producer_b)
        with pytest.raises(BusinessRuleException) as exc:
            run(svc_b.confirm_delivery_and_payment(producer_phone="+22670000002", order_id=str(order.id)))
        assert exc.value.reason == "not_owner"


class TestPreorderNonEscrowJourneyToCompletion:
    def test_confirmed_preorder_order_reaches_completion_via_orderitem_ownership(self):
        """Reprend la FORME exacte que `confirm_preorder_draft` produit
        (`Order.status="CONFIRMED"`, `payment_status`/`delivery_status`
        restés à leur défaut `PENDING`, un `OrderItem` réel liant un
        `Product` à son producteur — voir `services/database/buyer.py`,
        déjà exhaustivement testé séparément pour sa propre logique
        palier/prix) — vérifie que la clôture F1 s'y raccroche correctement
        SANS toucher au chemin escrow (mandat §9 : chemin non modifié)."""
        buyer_phone = "+22670000050"
        producer_phone = "+22670000051"
        producer = types.SimpleNamespace(id=uuid.uuid4())

        order = types.SimpleNamespace(
            id=uuid.uuid4(), buyer_id=uuid.uuid4(), winning_bid_id=None,
            status="CONFIRMED", payment_status="PENDING", delivery_status="PENDING",
            total_amount=12500.0, currency="XOF", confirmed_at=None,
        )

        session = _FakeConfirmDeliveryPaymentSession(
            order, owns_via_items=True, buyer_phone=buyer_phone
        )
        svc = _producer_svc(session, producer)

        result = run(
            svc.confirm_delivery_and_payment(producer_phone=producer_phone, order_id=str(order.id))
        )

        assert result["outcome"] == "COMPLETED"
        assert order.status == "COMPLETED"
        assert order.payment_status == "PAID"
        assert order.delivery_status == "DELIVERED"

    def test_an_escrow_confirmed_order_is_untouched_by_this_path(self):
        """Non-régression explicite du mandat §9/§34 : un préorder ESCROW
        (`payment_status="ESCROWED"`, atteint via `initiate_escrow_payment`,
        JAMAIS `PENDING`) ne doit jamais pouvoir être clôturé par ce
        nouveau chemin — seul `EscrowMixin.verify_delivery_otp` le peut."""
        producer = types.SimpleNamespace(id=uuid.uuid4())
        order = types.SimpleNamespace(
            id=uuid.uuid4(), buyer_id=uuid.uuid4(), winning_bid_id=None,
            status="CONFIRMED", payment_status="ESCROWED", delivery_status="PENDING",
            total_amount=12500.0, currency="XOF", confirmed_at=None,
        )
        session = _FakeConfirmDeliveryPaymentSession(order, owns_via_items=True)
        svc = _producer_svc(session, producer)

        with pytest.raises(BusinessRuleException) as exc:
            run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))
        assert exc.value.reason == "not_pay_at_delivery"
        assert order.payment_status == "ESCROWED"  # jamais altéré
        assert order.status == "CONFIRMED"
