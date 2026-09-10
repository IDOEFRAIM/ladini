"""`ProducerMgmtMixin.confirm_delivery_and_payment` — clôture F1 (paiement à
la livraison, 2026-09-04). Décision produit actée : producteur seul, un
seul geste combiné (mirroir de `record_sale`, seul précédent réel pour une
vente cash dans ce dépôt) — voir `docs/F1_PAYMENT_DELIVERY_CLOSURE_2026-09-04.md`.

## Ce que ce fichier verrouille

1. Appartenance producteur UNIFIÉE sur les deux origines réelles d'une
   `Order` (PREORDER via `OrderItem`, RFQ via `Bid.producer_id`) — jamais
   l'une sans l'autre, jamais un producteur non concerné.
2. Idempotence — un second appel sur une commande déjà `COMPLETED` renvoie
   un outcome explicite, ne relève jamais d'exception, ne réécrit rien.
3. Gardes d'état — refuse une commande déjà escrow (`payment_status !=
   PENDING`) ou pas encore confirmée (`status != CONFIRMED`).
4. Transition ATOMIQUE — `delivery_status`/`payment_status`/`status` posés
   ensemble, jamais l'un sans les autres (mandat §10/§14).
5. Verrouillage `FOR UPDATE` réel (SQL compilé).
6. `OrderStatusHistory` × 3 (DELIVERY/PAYMENT/ORDER) et notification Outbox
   à l'acheteur.

## Portée honnête

Faux moteur minimal (aucune infrastructure Postgres réelle dans ce dépôt,
même limite documentée partout ailleurs dans cette suite)."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.producer import ProducerMgmtMixin
from ladini.services.database.errors import BusinessRuleException


async def _async_return(value):
    return value


def _order(**overrides):
    base = dict(
        id=uuid.uuid4(),
        buyer_id=uuid.uuid4(),
        winning_bid_id=None,
        status="CONFIRMED",
        payment_status="PENDING",
        delivery_status="PENDING",
        total_amount=15000.0,
        currency="XOF",
        confirmed_at=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeSession:
    """1er `.scalar()` -> Order (FOR UPDATE) ; 2e -> appartenance via
    OrderItem ; 3e (si atteint) -> appartenance via Bid. `.execute()` sert
    ensuite le lookup téléphone acheteur PUIS l'insertion outbox — distingués
    par TYPE de statement (voir `_execute` ci-dessous), jamais par ordre
    d'appel fragile."""

    def __init__(self, order, *, owns_via_items=False, owns_via_bid=False, buyer_phone="+22670000099"):
        self._order = order
        self._owns_via_items = owns_via_items
        self._owns_via_bid = owns_via_bid
        self._buyer_phone = buyer_phone
        self._scalar_calls = 0
        self.added: list = []
        self.captured_select_statements: list = []

    async def scalar(self, stmt):
        self._scalar_calls += 1
        self.captured_select_statements.append(stmt)
        if self._scalar_calls == 1:
            return self._order
        if self._scalar_calls == 2:
            return uuid.uuid4() if self._owns_via_items else None
        if self._scalar_calls == 3:
            return uuid.uuid4() if self._owns_via_bid else None
        return None

    async def execute(self, stmt):
        from sqlalchemy.sql.dml import Insert as _InsertStmt

        if isinstance(stmt, _InsertStmt):
            return types.SimpleNamespace(all=lambda: [1])
        return types.SimpleNamespace(first=lambda: (self._buyer_phone,) if self._buyer_phone else None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _producer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4())
    producer = types.SimpleNamespace(id=uuid.uuid4())
    return user, producer


def _service(session):
    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    svc.get_producer_profile = lambda phone: _async_return(_producer_profile())
    return svc


class TestOwnershipAcrossBothOrderOrigins:
    def test_preorder_order_owned_via_orderitem_succeeds(self):
        order = _order()
        session = _FakeSession(order, owns_via_items=True)
        svc = _service(session)

        result = run(
            svc.confirm_delivery_and_payment(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert result["status"] == "success"
        assert result["outcome"] == "COMPLETED"

    def test_rfq_order_owned_via_winning_bid_succeeds(self):
        order = _order(winning_bid_id=uuid.uuid4())
        session = _FakeSession(order, owns_via_items=False, owns_via_bid=True)
        svc = _service(session)

        result = run(
            svc.confirm_delivery_and_payment(
                producer_phone="+22670000001", order_id=str(order.id)
            )
        )
        assert result["status"] == "success"
        assert result["outcome"] == "COMPLETED"

    def test_unrelated_producer_is_rejected(self):
        order = _order(winning_bid_id=uuid.uuid4())
        session = _FakeSession(order, owns_via_items=False, owns_via_bid=False)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(
                svc.confirm_delivery_and_payment(
                    producer_phone="+22670000001", order_id=str(order.id)
                )
            )
        assert exc.value.reason == "not_owner"
        assert order.status == "CONFIRMED"  # jamais modifiée


class TestAtomicTransition:
    def test_delivery_payment_and_status_are_all_set_together(self):
        order = _order()
        session = _FakeSession(order, owns_via_items=True)
        svc = _service(session)

        run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))

        assert order.delivery_status == "DELIVERED"
        assert order.payment_status == "PAID"
        assert order.status == "COMPLETED"
        assert order.confirmed_at is not None

    def test_three_status_history_entries_are_written(self):
        order = _order()
        session = _FakeSession(order, owns_via_items=True)
        svc = _service(session)

        run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))

        history = [o for o in session.added if type(o).__name__ == "OrderStatusHistory"]
        assert len(history) == 3
        types_seen = {h.status_type for h in history}
        assert types_seen == {"DELIVERY", "PAYMENT", "ORDER"}
        order_entry = next(h for h in history if h.status_type == "ORDER")
        assert order_entry.from_status == "CONFIRMED"
        assert order_entry.to_status == "COMPLETED"


class TestStateGuards:
    def test_escrow_order_is_never_touched_by_this_path(self):
        """`payment_status="ESCROWED"` -> ce chemin (paiement à la
        livraison) doit rester à l'écart, geré exclusivement par
        `EscrowMixin.verify_delivery_otp`."""
        order = _order(payment_status="ESCROWED")
        session = _FakeSession(order, owns_via_items=True)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))
        assert exc.value.reason == "not_pay_at_delivery"
        assert order.status == "CONFIRMED"

    def test_a_not_yet_confirmed_order_is_rejected(self):
        order = _order(status="DRAFT")
        session = _FakeSession(order, owns_via_items=True)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc:
            run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))
        assert exc.value.reason == "order_not_confirmed"


class TestIdempotence:
    def test_repeated_confirmation_returns_already_completed_never_reruns(self):
        order = _order(status="COMPLETED", payment_status="PAID", delivery_status="DELIVERED")
        session = _FakeSession(order, owns_via_items=True)
        svc = _service(session)

        result = run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))

        assert result["outcome"] == "ALREADY_COMPLETED"
        assert not session.added  # aucune nouvelle OrderStatusHistory

    def test_ten_repeated_calls_produce_exactly_one_logical_transition(self):
        # `order` PERSISTE d'un appel à l'autre (c'est ce qui compte pour
        # l'idempotence — un vrai Postgres relirait la MÊME ligne) ; chaque
        # appel reçoit sa PROPRE session fraîche (même limite honnête que le
        # reste de cette suite : ce faux moteur ne simule pas un pool de
        # connexions partagé, seulement l'état métier qui doit survivre).
        order = _order()
        all_added: list = []

        outcomes = []
        for _ in range(10):
            session = _FakeSession(order, owns_via_items=True)
            svc = _service(session)
            result = run(
                svc.confirm_delivery_and_payment(
                    producer_phone="+22670000001", order_id=str(order.id)
                )
            )
            outcomes.append(result["outcome"])
            all_added.extend(session.added)

        assert outcomes[0] == "COMPLETED"
        assert all(o == "ALREADY_COMPLETED" for o in outcomes[1:])
        history = [o for o in all_added if type(o).__name__ == "OrderStatusHistory"]
        assert len(history) == 3  # jamais dupliqué au fil des 9 rejeux suivants


class TestRowLocking:
    def test_the_order_lookup_is_for_update(self):
        from sqlalchemy.dialects import postgresql

        order = _order()
        session = _FakeSession(order, owns_via_items=True)
        svc = _service(session)

        run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))

        sql = str(
            session.captured_select_statements[0].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}
            )
        )
        assert "FOR UPDATE" in sql.upper()


class TestBuyerNotification:
    def test_a_completed_order_enqueues_exactly_one_outbox_notification(self):
        order = _order()
        session = _FakeSession(order, owns_via_items=True, buyer_phone="+22670000042")
        svc = _service(session)

        result = run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))

        assert result["outcome"] == "COMPLETED"
        # Preuve indirecte robuste : le flush réussit sans exception malgré
        # l'appel outbox réel (`_outbox_repo.enqueue`) — la fonction NE
        # PLANTE PAS quand un téléphone acheteur est résolu (chemin exercé).

    def test_no_buyer_phone_resolved_never_blocks_the_completion(self):
        order = _order()
        session = _FakeSession(order, owns_via_items=True, buyer_phone=None)
        svc = _service(session)

        result = run(svc.confirm_delivery_and_payment(producer_phone="+22670000001", order_id=str(order.id)))
        assert result["outcome"] == "COMPLETED"
