"""CONFIRMATION GROUPÉE d'un checkout multi-producteurs (Phase 6A, UX A3).

L'acheteur ne confirme qu'UNE fois ; toutes les commandes du même
`checkout_group_id` passent `DRAFT → CONFIRMED` dans la MÊME transaction.
Chaque producteur reçoit ensuite une notification portant le montant de SA
commande — jamais le total du checkout.

`checkout_group_id` reste une simple corrélation : après confirmation,
chaque commande vit indépendamment (prouvé dans
`test_multi_producer_checkout.py`)."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.buyer import BuyerMixin
from ladini.services.database.errors import BusinessRuleException


async def _async_return(value):
    return value


PRODUCER_A = uuid.uuid4()
PRODUCER_B = uuid.uuid4()
PHONE_A = "+22670000001"
PHONE_B = "+22670000002"


def _product(producer_id, name, qty=500.0):
    return types.SimpleNamespace(
        id=uuid.uuid4(), name=name, quantity_for_sale=qty, unit="KG",
        producer_id=producer_id,
    )


def _item(product, qty=2.0, price=1000.0):
    return types.SimpleNamespace(
        product_id=product.id, quantity=qty, price_at_sale=price,
        base_unit_quantity=None, product=product,
    )


def _draft_order(items, group_id):
    return types.SimpleNamespace(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), status="DRAFT",
        items=items, checkout_group_id=group_id, preorder_converted_at=None,
        payment_status=None, subtotal=0.0, total_amount=0.0, currency="XOF",
        gps_lat=None, gps_lng=None,
    )


class _GroupSession:
    def __init__(self, primary, siblings, phone_by_producer):
        self._primary = primary
        self._siblings = siblings
        self._phones = phone_by_producer
        self._primary_served = False

    def _sql(self, stmt):
        from sqlalchemy.dialects import postgresql

        return str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )

    async def scalar(self, stmt):
        sql = self._sql(stmt)
        if "marketplace.products" in sql:
            for order in [self._primary, *self._siblings]:
                for item in order.items:
                    if str(item.product_id) in sql:
                        return item.product
            return None
        if "marketplace.orders" in sql and not self._primary_served:
            self._primary_served = True
            return self._primary
        return None

    async def execute(self, stmt):
        sql = self._sql(stmt)
        if "marketplace.orders" in sql and "checkout_group_id" in sql:
            siblings = self._siblings
            return types.SimpleNamespace(
                scalars=lambda: types.SimpleNamespace(
                    unique=lambda: types.SimpleNamespace(all=lambda: siblings)
                )
            )
        for producer_id, phone in self._phones.items():
            if str(producer_id) in sql:
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
    svc.get_buyer_profile = lambda phone=None: _async_return(
        (
            types.SimpleNamespace(id=uuid.uuid4(), zone_id=None),
            types.SimpleNamespace(id=uuid.uuid4()),
        )
    )
    return svc


@pytest.fixture()
def captured_outbox(monkeypatch):
    captured: list = []

    async def _fake_enqueue(_session, entries):
        captured.extend(entries)
        return len(entries)

    monkeypatch.setattr(
        "ladini.workers.repositories.outbox_repo.enqueue", _fake_enqueue
    )
    return captured


def _two_producer_group():
    group_id = uuid.uuid4()
    prod_a = _product(PRODUCER_A, "Riz")
    prod_b = _product(PRODUCER_B, "Tomate")
    order_a = _draft_order([_item(prod_a, qty=2.0, price=1000.0)], group_id)   # 2000
    order_b = _draft_order([_item(prod_b, qty=4.0, price=500.0)], group_id)    # 2000
    return order_a, order_b, prod_a, prod_b


class TestGroupedConfirmation:
    def test_one_confirmation_confirms_every_order_of_the_group(self, captured_outbox):
        order_a, order_b, _, _ = _two_producer_group()
        session = _GroupSession(order_a, [order_b], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})

        result = run(
            _service(session).confirm_preorder_draft(
                buyer_phone="+22670000099", preorder_id=str(order_a.id)
            )
        )

        assert result["status"] == "success"
        # (2026-09-13, confirmation explicite producteur) : la précommande
        # confirmée par l'acheteur entre en attente de confirmation DU
        # PRODUCTEUR — elle ne devient "CONFIRMED" qu'après son geste
        # explicite (`ProducerMgmtMixin.confirm_order_by_producer`).
        assert order_a.status == "PENDING_PRODUCER_CONFIRMATION"
        assert order_b.status == "PENDING_PRODUCER_CONFIRMATION"
        assert len(result["orders"]) == 2

    def test_each_order_keeps_its_own_total(self, captured_outbox):
        order_a, order_b, _, _ = _two_producer_group()
        session = _GroupSession(order_a, [order_b], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})

        result = run(
            _service(session).confirm_preorder_draft(
                buyer_phone="+22670000099", preorder_id=str(order_a.id)
            )
        )

        assert float(order_a.total_amount) == 2000.0
        assert float(order_b.total_amount) == 2000.0
        assert result["checkout_total"] == 4000.0

    def test_stock_is_debited_once_per_product(self, captured_outbox):
        order_a, order_b, prod_a, prod_b = _two_producer_group()
        session = _GroupSession(order_a, [order_b], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})

        run(
            _service(session).confirm_preorder_draft(
                buyer_phone="+22670000099", preorder_id=str(order_a.id)
            )
        )
        assert prod_a.quantity_for_sale == 498.0   # 500 - 2
        assert prod_b.quantity_for_sale == 496.0   # 500 - 4


class TestNotificationsArePerOrder:
    def test_each_producer_is_notified_once_with_his_own_amount(self, captured_outbox):
        order_a, order_b, _, _ = _two_producer_group()
        session = _GroupSession(order_a, [order_b], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})

        run(
            _service(session).confirm_preorder_draft(
                buyer_phone="+22670000099", preorder_id=str(order_a.id)
            )
        )

        by_phone = {}
        for entry in captured_outbox:
            assert entry["template_key"] == "PREORDER_CONFIRMED_PRODUCER"
            by_phone.setdefault(entry["recipient_phone"], []).append(entry)

        assert set(by_phone) == {PHONE_A, PHONE_B}
        assert len(by_phone[PHONE_A]) == 1, "exactement une notification"
        assert len(by_phone[PHONE_B]) == 1
        # Jamais le total du checkout (4000) — chacun voit SA part.
        assert by_phone[PHONE_A][0]["payload"]["amount"] == 2000.0
        assert by_phone[PHONE_B][0]["payload"]["amount"] == 2000.0
        # Chacun reçoit le numéro de SA commande.
        assert by_phone[PHONE_A][0]["payload"]["order_number"] == str(order_a.id)[:8].upper()
        assert by_phone[PHONE_B][0]["payload"]["order_number"] == str(order_b.id)[:8].upper()

    def test_dedupe_keys_are_per_order_and_phone(self, captured_outbox):
        order_a, order_b, _, _ = _two_producer_group()
        session = _GroupSession(order_a, [order_b], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})
        run(
            _service(session).confirm_preorder_draft(
                buyer_phone="+22670000099", preorder_id=str(order_a.id)
            )
        )
        keys = {e["dedupe_key"] for e in captured_outbox}
        assert len(keys) == 2
        assert all(k.startswith("PREORDER_CONFIRMED_PRODUCER:") for k in keys)


class TestIdempotenceAndAtomicity:
    def test_confirming_twice_never_re_debits_nor_re_notifies(self, captured_outbox):
        order_a, order_b, prod_a, prod_b = _two_producer_group()
        session = _GroupSession(order_a, [order_b], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})
        run(
            _service(session).confirm_preorder_draft(
                buyer_phone="+22670000099", preorder_id=str(order_a.id)
            )
        )
        assert len(captured_outbox) == 2
        assert prod_a.quantity_for_sale == 498.0

        # 2e confirmation sur les MÊMES objets métier (déjà CONFIRMED),
        # session fraîche.
        session2 = _GroupSession(order_a, [order_b], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})
        with pytest.raises(BusinessRuleException) as exc:
            run(
                _service(session2).confirm_preorder_draft(
                    buyer_phone="+22670000099", preorder_id=str(order_a.id)
                )
            )
        assert exc.value.reason == "not_draft"
        assert len(captured_outbox) == 2, "aucune notification supplémentaire"
        assert prod_a.quantity_for_sale == 498.0, "aucun débit supplémentaire"

    def test_insufficient_stock_on_one_order_aborts_the_whole_group(self, captured_outbox):
        """Atomicité : si la part de B ne peut pas être servie, RIEN n'est
        confirmé — l'exception propage et la transaction est annulée. Aucune
        commande ne reste `DRAFT` à moitié confirmée."""
        order_a, order_b, prod_a, prod_b = _two_producer_group()
        prod_b.quantity_for_sale = 1.0  # insuffisant pour 4
        session = _GroupSession(order_a, [order_b], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})

        with pytest.raises(BusinessRuleException) as exc:
            run(
                _service(session).confirm_preorder_draft(
                    buyer_phone="+22670000099", preorder_id=str(order_a.id)
                )
            )
        assert exc.value.reason == "insufficient_stock"
        assert order_a.status == "DRAFT"
        assert order_b.status == "DRAFT"
        assert not captured_outbox


class TestSingleProducerUnchanged:
    def test_a_lone_order_without_group_behaves_exactly_as_before(self, captured_outbox):
        prod = _product(PRODUCER_A, "Riz")
        order = _draft_order([_item(prod, qty=3.0, price=1000.0)], None)
        session = _GroupSession(order, [], {PRODUCER_A: PHONE_A})

        result = run(
            _service(session).confirm_preorder_draft(
                buyer_phone="+22670000099", preorder_id=str(order.id)
            )
        )

        # (2026-09-13, confirmation explicite producteur) : voir note dans
        # TestGroupedConfirmation ci-dessus.
        assert order.status == "PENDING_PRODUCER_CONFIRMATION"
        assert float(order.total_amount) == 3000.0
        assert len(result["orders"]) == 1
        assert "une par producteur" not in result["message"]
        assert len(captured_outbox) == 1


class TestLegacyMultiProducerOrdersAreGrandfathered:
    def test_an_old_order_without_group_still_confirms_and_notifies_both(
        self, captured_outbox
    ):
        """Commande ANCIENNE (avant le split) : `checkout_group_id` NULL mais
        deux producteurs dans la même commande. Elle doit continuer à
        fonctionner — et la collecte multi-producteurs par commande reste
        donc nécessaire."""
        prod_a = _product(PRODUCER_A, "Riz")
        prod_b = _product(PRODUCER_B, "Tomate")
        legacy = _draft_order(
            [_item(prod_a, qty=2.0, price=1000.0), _item(prod_b, qty=2.0, price=500.0)],
            None,  # pas de groupe : commande héritée
        )
        session = _GroupSession(legacy, [], {PRODUCER_A: PHONE_A, PRODUCER_B: PHONE_B})

        run(
            _service(session).confirm_preorder_draft(
                buyer_phone="+22670000099", preorder_id=str(legacy.id)
            )
        )

        # (2026-09-13, confirmation explicite producteur) : voir note dans
        # TestGroupedConfirmation ci-dessus.
        assert legacy.status == "PENDING_PRODUCER_CONFIRMATION"
        assert float(legacy.total_amount) == 3000.0
        phones = {e["recipient_phone"] for e in captured_outbox}
        assert phones == {PHONE_A, PHONE_B}
