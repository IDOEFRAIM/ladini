"""`BuyerMixin.confirm_preorder_draft` — notification producteur, chemin
SANS escrow (F2, 2026-09-04, audit fonctionnel — "producteur jamais notifié
d'une vente préorder directe").

## Le gap réel fermé par ce fichier

`confirm_preorder_draft` (paiement à la livraison,
`ESCROW_PAYMENT_ENABLED=False`) n'enfilait AUCUNE notification producteur —
contrairement au chemin escrow (`EscrowMixin.mark_escrow_paid`, déjà
notifiait via `ESCROW_PAYMENT_SECURED_PRODUCER`). Réutilise EXACTEMENT le
même motif de collecte multi-producteurs que `mark_escrow_paid` (jamais
dupliqué, jamais réinventé) — nouveau template `PREORDER_CONFIRMED_PRODUCER`
qui ne prétend JAMAIS qu'un paiement a été reçu (payment at delivery)."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.buyer import BuyerMixin
from ladini.services.database.errors import BusinessRuleException


class _Rows(list):
    """Liste de tuples-ligne qui supporte AUSSI `.first()`/`.all()` — voir la
    même classe dans test_cancel_pending_order_confirmed_gap.py pour le
    détail (protocole d'itération vs `types.SimpleNamespace`)."""

    def first(self):
        return self[0] if self else None

    def all(self):
        return list(self)


async def _async_return(value):
    return value


def _fake_buyer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4(), zone_id=None)
    profile = types.SimpleNamespace(id=uuid.uuid4())
    return user, profile


def _product(producer_id, qty=1000.0):
    return types.SimpleNamespace(
        id=uuid.uuid4(), name="Riz", quantity_for_sale=qty, unit="KG",
        producer_id=producer_id,
    )


def _item(product, price=250.0, qty=10.0):
    return types.SimpleNamespace(
        product_id=product.id, quantity=qty, price_at_sale=price,
        base_unit_quantity=None, product=product,
    )


class _FakeSession:
    """1er `.scalar()` -> Order (FOR UPDATE), suivants -> Product par item
    (matché par id dans le SQL compilé). `.execute()` distingue le SELECT
    téléphone producteur (retourne `phones_by_producer`) de l'INSERT outbox
    (compté, jamais exécuté réellement)."""

    def __init__(self, order, phones_by_producer):
        self._order = order
        self._phones_by_producer = phones_by_producer
        self._n = 0
        self.outbox_inserts: list = []

    async def scalar(self, stmt):
        self._n += 1
        if self._n == 1:
            return self._order
        from sqlalchemy.dialects import postgresql

        sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for item in self._order.items:
            if str(item.product_id) in sql:
                return item.product
        return None

    async def execute(self, stmt):
        from sqlalchemy.sql.dml import Insert as _InsertStmt
        from sqlalchemy.dialects import postgresql

        if isinstance(stmt, _InsertStmt):
            self.outbox_inserts.append(stmt)
            return _Rows([1])
        sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        # Le service résout les téléphones producteurs en UNE requête
        # groupée (`Producer.id.in_(...)`) — ce double est donc lui aussi
        # ensembliste, jamais "une ligne par appel".
        return _Rows(
            [
                (producer_id, phone)
                for producer_id, phone in self._phones_by_producer.items()
                if str(producer_id) in sql
            ]
        )

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


def _draft_order(items):
    return types.SimpleNamespace(
        id=uuid.uuid4(), buyer_id=uuid.uuid4(), status="DRAFT",
        items=items, preorder_converted_at=None, payment_status=None,
        subtotal=0.0, total_amount=0.0, currency="XOF",
        gps_lat=None, gps_lng=None,
        # (2026-09-05, Phase 6A) `Order.checkout_group_id` : NULL = commande
        # hors checkout groupé. Ces scénarios restent volontairement des
        # commandes UNIQUES portant plusieurs producteurs — c'est-à-dire le
        # cas des commandes ANTÉRIEURES au split (grandfathering), qui doit
        # continuer de notifier chaque producteur exactement une fois.
        checkout_group_id=None,
    )


class TestNonEscrowConfirmationNotifiesProducerExactlyOnce:
    def test_a_single_producer_order_enqueues_exactly_one_notification(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id)
        order = _draft_order([_item(product)])
        session = _FakeSession(order, {producer_id: "+22670000001"})
        svc = _service(session)

        result = run(
            svc.confirm_preorder_draft(buyer_phone="+22670000099", preorder_id=str(order.id))
        )

        assert result["status"] == "success"
        assert len(session.outbox_inserts) == 1
        # (2026-09-13, incident WhatsApp #3) : ce process MCP n'a pas accès
        # à Redis — c'est à l'appelant côté worker de poser l'indice de
        # rôle "prochaine réponse = PRODUCER" pour chacun de ces numéros
        # (voir flows/buyer/preorder_confirmation.py) ; ce résultat doit
        # donc lister EXACTEMENT les numéros notifiés ci-dessus.
        assert result["producer_phones_notified"] == ["+22670000001"]

    def test_retry_after_confirmation_never_re_notifies(self):
        """`confirm_preorder_draft` × 2 — la 2e tentative échoue AVANT
        d'atteindre le code de notification (`order.status != "DRAFT"`,
        déjà `CONFIRMED` par la 1re) : idempotence NATURELLE, aucune
        nouvelle primitive nécessaire (mandat §4)."""
        producer_id = uuid.uuid4()
        product = _product(producer_id)
        order = _draft_order([_item(product)])
        session = _FakeSession(order, {producer_id: "+22670000001"})
        svc = _service(session)

        run(svc.confirm_preorder_draft(buyer_phone="+22670000099", preorder_id=str(order.id)))
        assert len(session.outbox_inserts) == 1

        # order.status est maintenant "CONFIRMED" (mutation réelle sur le
        # MÊME objet Python, comme une vraie ligne rechargée en base) — le
        # retry reçoit sa PROPRE session fraîche (ce faux moteur ne simule
        # pas un pool de connexions partagé, seulement l'état métier qui
        # doit survivre — même limite honnête que le reste de cette suite).
        session2 = _FakeSession(order, {producer_id: "+22670000001"})
        svc2 = _service(session2)
        with pytest.raises(BusinessRuleException) as exc:
            run(svc2.confirm_preorder_draft(buyer_phone="+22670000099", preorder_id=str(order.id)))
        assert exc.value.reason == "not_draft"
        assert not session2.outbox_inserts  # jamais une 2e notification

    def test_multi_producer_cart_notifies_each_distinct_producer_once(self):
        producer_a = uuid.uuid4()
        producer_b = uuid.uuid4()
        product_a = _product(producer_a)
        product_b = _product(producer_b)
        order = _draft_order([_item(product_a), _item(product_b)])
        session = _FakeSession(order, {producer_a: "+22670000001", producer_b: "+22670000002"})
        svc = _service(session)

        result = run(
            svc.confirm_preorder_draft(buyer_phone="+22670000099", preorder_id=str(order.id))
        )

        assert result["status"] == "success"
        assert len(session.outbox_inserts) == 1  # un seul appel enqueue() portant les 2 entrées
        assert sorted(result["producer_phones_notified"]) == [
            "+22670000001",
            "+22670000002",
        ]

    def test_no_producer_phone_resolved_never_blocks_confirmation(self):
        producer_id = uuid.uuid4()
        product = _product(producer_id)
        order = _draft_order([_item(product)])
        session = _FakeSession(order, {})  # aucun téléphone résolu
        svc = _service(session)

        result = run(
            svc.confirm_preorder_draft(buyer_phone="+22670000099", preorder_id=str(order.id))
        )
        assert result["status"] == "success"
        assert not session.outbox_inserts
        assert result["producer_phones_notified"] == []
