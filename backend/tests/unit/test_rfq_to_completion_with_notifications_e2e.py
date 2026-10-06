"""TEST E2E FINAL — RFQ → Winner → Notifications (F3) → Order → Fulfillment
(F1), 2026-09-04, mandat §22.

```
Buyer    -> RFQ (auction OPEN)
Producer A -> bid 250
Producer B -> bid 275
Producer A -> update to 300
Buyer    -> select A
  -> A reçoit AUCTION_WON_PRODUCER
  -> B reçoit AUCTION_LOST_PRODUCER
  -> Order CONFIRMED (payment=PENDING, delivery=PENDING)
Producer A -> confirme livraison + paiement à la livraison
  -> Order COMPLETED (payment=PAID, delivery=DELIVERED)
```

Démontre la continuité RFQ → Winner → Order → Fulfillment avec les DEUX
correctifs de cette session (F1 : clôture paiement-à-la-livraison ; F3 :
notification des perdants) enchaînés sur le MÊME scénario, en réutilisant
les vrais services (`AuctionMixin`, `ProducerMgmtMixin`) — mêmes objets
Python d'une étape à l'autre, même convention que
`test_auction_bid_full_lifecycle_e2e.py`/`test_payment_at_delivery_e2e.py`."""
from __future__ import annotations

import types
import uuid

from ladini.services.database.auction import AuctionMixin
from ladini.services.database.producer import ProducerMgmtMixin
from tests.conftest import run


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


class _FakeSingleBidSession:
    def __init__(self, bid):
        self._bid = bid

    async def scalar(self, _stmt):
        return self._bid

    async def get(self, _model, _pk):
        # B2b : corriger le prix d'un bid CERTIFIÉ recharge l'enchère (quantité/unité) pour recalculer son snapshot.
        import types as _types

        return _types.SimpleNamespace(id=self._bid.auction_id, quantity=10.0, unit="TONNE", buyer_id=None)

    async def flush(self):
        pass


class _FakeSelectWinningBidSession:
    def __init__(self, row, loser_producer_ids, phone_by_producer, owner_phone=None):
        from sqlalchemy.sql.dml import Update as _UpdateStmt

        self._row = row
        self._UpdateStmt = _UpdateStmt
        self._loser_producer_ids = list(loser_producer_ids)
        self._phone_by_producer = phone_by_producer
        self._owner_phone = owner_phone
        self._select_served = False
        self.added: list = []

    async def scalar(self, stmt):
        return self._owner_phone

    async def execute(self, stmt):
        if isinstance(stmt, self._UpdateStmt):
            ids = self._loser_producer_ids
            return types.SimpleNamespace(scalars=lambda: types.SimpleNamespace(all=lambda: ids))
        if not self._select_served:
            self._select_served = True
            row = self._row
            return types.SimpleNamespace(fetchone=lambda: row)
        rows = [(pid, self._phone_by_producer.get(pid)) for pid in self._loser_producer_ids]
        return types.SimpleNamespace(all=lambda: rows)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


class _FakeConfirmDeliveryPaymentSession:
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


class TestRfqWinnerNotificationsThenFulfillment:
    def test_full_scenario(self, monkeypatch):
        captured_outbox: list = []

        async def _fake_enqueue(_session, entries):
            captured_outbox.extend(entries)
            return len(entries)

        monkeypatch.setattr(
            "ladini.workers.repositories.outbox_repo.enqueue", _fake_enqueue
        )

        buyer_phone = "+22670000099"
        phone_a, phone_b = "+22670000001", "+22670000002"
        producer_a = types.SimpleNamespace(id=uuid.uuid4())
        producer_b = types.SimpleNamespace(id=uuid.uuid4())
        auction = _auction()

        # ── PRODUCER A -> bid 250 ──
        svc_a = _auction_svc(_FakePlaceBidSession(auction, None), producer=producer_a)
        session_a = svc_a.session
        run(svc_a.place_bid(auction_id=str(auction.id), phone=phone_a, offered_price=250.0, price_basis="PER_BASE_UNIT", price_unit="TONNE"))
        bid_a = session_a.added[0]

        # ── PRODUCER B -> bid 275 ──
        svc_b = _auction_svc(_FakePlaceBidSession(auction, None), producer=producer_b)
        session_b = svc_b.session
        run(svc_b.place_bid(auction_id=str(auction.id), phone=phone_b, offered_price=275.0, price_basis="PER_BASE_UNIT", price_unit="TONNE"))
        bid_b = session_b.added[0]

        # ── PRODUCER A -> update to 300 ──
        svc_a2 = _auction_svc(_FakeSingleBidSession(bid_a))
        run(svc_a2.update_bid_price(bid_id=str(bid_a.id), phone=phone_a, new_price=300.0))
        assert bid_a.offered_price == 300.0

        # ── BUYER -> select A (winner) ──
        sub_cat = types.SimpleNamespace(name="Riz")
        row = (bid_a, auction, "Producteur A", phone_a, sub_cat)
        select_session = _FakeSelectWinningBidSession(
            row,
            loser_producer_ids=[producer_b.id],
            phone_by_producer={producer_b.id: phone_b},
            owner_phone=buyer_phone,
        )
        svc_buyer = _auction_svc(select_session)
        win = run(svc_buyer.select_winning_bid(bid_id=str(bid_a.id), phone=buyer_phone))
        assert win["status"] == "success"
        order = next(o for o in select_session.added if type(o).__name__ == "Order")
        assert order.total_amount == 300.0 * 10.0

        # ── Notifications F3 : A gagne, B perd, exactement une fois chacun ──
        winner_entries = [e for e in captured_outbox if e["template_key"] == "AUCTION_WON_PRODUCER"]
        loser_entries = [e for e in captured_outbox if e["template_key"] == "AUCTION_LOST_PRODUCER"]
        assert len(winner_entries) == 1
        assert winner_entries[0]["recipient_phone"] == phone_a
        assert len(loser_entries) == 1
        assert loser_entries[0]["recipient_phone"] == phone_b

        # Défauts de colonne non simulés par ce faux moteur (voir les
        # fichiers jumeaux de cette suite) — reproduits explicitement.
        order.payment_status = order.payment_status or "PENDING"
        order.delivery_status = order.delivery_status or "PENDING"
        assert order.status == "CONFIRMED"

        # ── F1 : PRODUCER A confirme livraison + paiement à la livraison ──
        confirm_session = _FakeConfirmDeliveryPaymentSession(
            order, owns_via_bid=True, buyer_phone=buyer_phone
        )
        svc_confirm = _producer_svc(confirm_session, producer_a)
        closure = run(
            svc_confirm.confirm_delivery_and_payment(
                producer_phone=phone_a, order_id=str(order.id)
            )
        )

        assert closure["outcome"] == "COMPLETED"
        assert order.status == "COMPLETED"
        assert order.payment_status == "PAID"
        assert order.delivery_status == "DELIVERED"

        # Notification de clôture (F1) enfilée EN PLUS des 2 notifications
        # de sélection du gagnant — jamais confondue avec elles.
        completion_entries = [
            e for e in captured_outbox if e["template_key"] == "ORDER_COMPLETED_AT_DELIVERY_BUYER"
        ]
        assert len(completion_entries) == 1
        assert completion_entries[0]["recipient_phone"] == buyer_phone
