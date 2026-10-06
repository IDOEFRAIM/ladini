"""CHECKOUT MULTI-PRODUCTEURS — une commande par producteur (Phase 6A,
2026-09-05).

## Le modèle fermé ici

Un panier peut légitimement mélanger plusieurs producteurs (le vendeur est
choisi PAR PRODUIT). Jusqu'ici tout atterrissait dans UNE commande, alors
que la responsabilité — livrer, encaisser, annuler — est PAR PRODUCTEUR.
Conséquence : un producteur pouvait clôturer ou annuler la part d'un
autre.

Désormais : **une commande par producteur**, corrélées par
`checkout_group_id` (simple corrélation de checkout, aucun cycle de vie
global). Les contrôles de propriété de `confirm_delivery_and_payment` (F1)
et `cancel_confirmed_order` (Phase 5) deviennent corrects PAR
CONSTRUCTION — aucun de ces deux chemins n'a été modifié.

Les commandes ANTÉRIEURES multi-producteurs sont volontairement conservées
telles quelles (`checkout_group_id IS NULL`) : voir
`docs/MULTI_PRODUCER_ORDER_DECISION_2026-09-05.md` §18."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.domain.models import Order, OrderItem
from ladini.services.database.buyer import BuyerMixin
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.producer import ProducerMgmtMixin


async def _async_return(value):
    return value


PRODUCER_A = uuid.uuid4()
PRODUCER_B = uuid.uuid4()


def _product(producer_id, name, price=1000.0, qty=500.0, sub_category_id=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(), name=name, price=price, quantity_for_sale=qty,
        unit="KG", producer_id=producer_id, pricing_tiers=None,
        sub_category_id=sub_category_id, images=[],
    )


class _CheckoutSession:
    """Faux moteur pour `create_preorder_draft` : sert les produits par id,
    collecte les objets ajoutés (Orders + OrderItems)."""

    def __init__(self, products):
        self._by_id = {str(p.id): p for p in products}
        self.added: list = []

    async def scalar(self, stmt):
        from sqlalchemy.dialects import postgresql

        sql = str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        for pid, product in self._by_id.items():
            if pid in sql:
                return product
        return None

    async def execute(self, stmt):
        return types.SimpleNamespace(first=lambda: None, all=lambda: [])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    # helpers
    def orders(self):
        return [o for o in self.added if isinstance(o, Order)]

    def items(self):
        return [i for i in self.added if isinstance(i, OrderItem)]


def _buyer_service(session):
    class _Svc(BuyerMixin):
        @property
        def session(self):
            return session

    svc = _Svc()
    user = types.SimpleNamespace(id=uuid.uuid4(), name="Acheteur", zone_id=None)
    profile = types.SimpleNamespace(id=uuid.uuid4(), establishment_name=None)
    svc.get_buyer_profile = lambda phone=None: _async_return((user, profile))
    return svc


def _checkout(products, cart_items):
    session = _CheckoutSession(products)
    svc = _buyer_service(session)
    result = run(
        svc.create_preorder_draft(buyer_phone="+22670000099", cart_items=cart_items)
    )
    return result, session


class TestCheckoutSplitsByProducer:
    def test_two_producers_produce_two_orders_sharing_one_group(self):
        a1 = _product(PRODUCER_A, "Riz")
        a2 = _product(PRODUCER_A, "Maïs")
        b1 = _product(PRODUCER_B, "Tomate")
        result, session = _checkout(
            [a1, a2, b1],
            [
                {"product_id": str(a1.id), "quantity": 2},
                {"product_id": str(a2.id), "quantity": 3},
                {"product_id": str(b1.id), "quantity": 4},
            ],
        )

        orders = session.orders()
        assert len(orders) == 2, "une commande par producteur"
        groups = {str(o.checkout_group_id) for o in orders}
        assert len(groups) == 1, "toutes issues du MÊME checkout"
        assert result["checkout_group_id"] in groups
        assert len(result["order_ids"]) == 2

    def test_no_order_ever_contains_another_producers_line(self):
        a1 = _product(PRODUCER_A, "Riz")
        b1 = _product(PRODUCER_B, "Tomate")
        _, session = _checkout(
            [a1, b1],
            [
                {"product_id": str(a1.id), "quantity": 2},
                {"product_id": str(b1.id), "quantity": 5},
            ],
        )

        by_order = {}
        for item in session.items():
            by_order.setdefault(str(item.order_id), []).append(str(item.product_id))
        assert len(by_order) == 2
        for product_ids in by_order.values():
            assert len(product_ids) == 1
        # chaque commande ne porte qu'un seul producteur
        assert {str(a1.id)} in [set(v) for v in by_order.values()]
        assert {str(b1.id)} in [set(v) for v in by_order.values()]

    def test_totals_are_per_order_never_the_checkout_total(self):
        """Corrige aussi le P3 : le producteur A ne doit jamais voir le
        montant de B. Échoue avec l'ancien modèle (une seule Order portant
        le total global)."""
        a1 = _product(PRODUCER_A, "Riz", price=1000.0)
        b1 = _product(PRODUCER_B, "Tomate", price=500.0)
        result, session = _checkout(
            [a1, b1],
            [
                {"product_id": str(a1.id), "quantity": 2},   # 2000
                {"product_id": str(b1.id), "quantity": 4},   # 2000
            ],
        )

        totals = sorted(float(o.total_amount) for o in session.orders())
        assert totals == [2000.0, 2000.0]
        assert result["total_amount"] == 4000.0  # total DU CHECKOUT, côté acheteur
        for order_payload in result["orders"]:
            assert order_payload["total_amount"] == 2000.0

    def test_single_producer_checkout_is_unchanged(self):
        """Non-régression du cas majoritaire : un seul producteur ⇒ une
        seule commande, exactement comme avant."""
        a1 = _product(PRODUCER_A, "Riz", price=1000.0)
        a2 = _product(PRODUCER_A, "Maïs", price=250.0)
        result, session = _checkout(
            [a1, a2],
            [
                {"product_id": str(a1.id), "quantity": 2},
                {"product_id": str(a2.id), "quantity": 4},
            ],
        )

        orders = session.orders()
        assert len(orders) == 1
        assert float(orders[0].total_amount) == 3000.0
        assert result["order_id"] == str(orders[0].id)
        assert len(result["order_ids"]) == 1
        assert "une par producteur" not in result["message"]

    def test_a_producer_whose_items_are_all_rejected_gets_no_empty_order(self):
        """Un article écarté (produit introuvable) ne doit pas laisser une
        commande vide derrière lui."""
        a1 = _product(PRODUCER_A, "Riz")
        result, session = _checkout(
            [a1],
            [
                {"product_id": str(a1.id), "quantity": 2},
                {"product_id": str(uuid.uuid4()), "quantity": 9},  # inexistant
            ],
        )
        assert len(session.orders()) == 1
        assert len(result["unresolved_items"]) == 1


class TestOwnershipIsNowCorrectByConstruction:
    """Le cœur du P1 : après split, les gardes existants suffisent — aucun
    des deux chemins de terminalisation n'a été modifié."""

    def _order_of(self, producer_id, product):
        item = types.SimpleNamespace(
            product_id=product.id, quantity=2.0, price_at_sale=1000.0,
            base_unit_quantity=None, product=product,
        )
        return types.SimpleNamespace(
            id=uuid.uuid4(), buyer_id=uuid.uuid4(), status="CONFIRMED",
            payment_status="PENDING", delivery_status="PENDING",
            items=[item], winning_bid_id=None, cancellation_role=None,
            delivery_desc=None, confirmed_at=None, total_amount=2000.0,
            currency="XOF", checkout_group_id=uuid.uuid4(),
        )

    def _producer_session(self, order, *, owns):
        class _S:
            def __init__(self):
                self.added = []
                self._served = False

            def _sql(self, stmt):
                from sqlalchemy.dialects import postgresql

                return str(
                    stmt.compile(
                        dialect=postgresql.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                )

            async def scalar(self, stmt):
                sql = self._sql(stmt)
                if "marketplace.order_items" in sql and "marketplace.products" in sql:
                    return uuid.uuid4() if owns else None
                if "marketplace.bids" in sql:
                    return None
                if "marketplace.products" in sql:
                    return order.items[0].product
                if "marketplace.orders" in sql and not self._served:
                    self._served = True
                    return order
                return None

            async def scalars(self, stmt):
                # (2026-09-21, audit latence — N+1) : `cancel_confirmed_order`
                # verrouille désormais tous les produits en une requête
                # `IN (...)` — cette commande n'a qu'une ligne, même produit
                # que `.scalar()` ci-dessus renvoyait déjà sans distinction.
                # `.all()` : le code réel appelle `(await session.scalars(...)).all()`.
                sql = self._sql(stmt)
                matched = [order.items[0].product] if "marketplace.products" in sql else []
                return types.SimpleNamespace(all=lambda: matched)

            async def execute(self, stmt):
                from sqlalchemy.sql.dml import Insert as _Insert

                if isinstance(stmt, _Insert):
                    return types.SimpleNamespace(all=lambda: [1])
                return types.SimpleNamespace(first=lambda: ("+22670000099",))

            def add(self, obj):
                self.added.append(obj)

            async def flush(self):
                pass

        return _S()

    def _producer_service(self, session, producer_id):
        class _Svc(ProducerMgmtMixin):
            @property
            def session(self):
                return session

        svc = _Svc()
        svc.get_producer_profile = lambda phone=None: _async_return(
            (types.SimpleNamespace(id=uuid.uuid4()), types.SimpleNamespace(id=producer_id))
        )
        return svc

    def test_producer_a_cannot_terminalize_producer_b_order(self):
        """RÉGRESSION F1 EXPLICITE — avec l'ancien modèle (une commande
        portant les lignes de A ET de B), ce contrôle passait pour A sur la
        part de B. Avec une commande par producteur, il est impossible."""
        order_b = self._order_of(PRODUCER_B, _product(PRODUCER_B, "Tomate"))
        session = self._producer_session(order_b, owns=False)
        svc = self._producer_service(session, PRODUCER_A)

        with pytest.raises(BusinessRuleException) as exc:
            run(
                svc.confirm_delivery_and_payment(
                    producer_phone="+22670000001", order_id=str(order_b.id)
                )
            )
        assert exc.value.reason == "not_owner"
        assert order_b.status == "CONFIRMED"
        assert order_b.payment_status == "PENDING"

    def test_producer_a_cannot_cancel_producer_b_order(self):
        order_b = self._order_of(PRODUCER_B, _product(PRODUCER_B, "Tomate"))
        session = self._producer_session(order_b, owns=False)
        svc = self._producer_service(session, PRODUCER_A)

        with pytest.raises(BusinessRuleException) as exc:
            run(
                svc.cancel_confirmed_order(
                    producer_phone="+22670000001", order_id=str(order_b.id)
                )
            )
        assert exc.value.reason == "not_owner"
        assert order_b.status == "CONFIRMED"

    def test_completing_one_order_never_touches_the_sibling(self):
        """A clôture SA commande ; celle de B, même `checkout_group_id`,
        reste `CONFIRMED`. Aucun cycle de vie global n'existe."""
        product_a = _product(PRODUCER_A, "Riz")
        order_a = self._order_of(PRODUCER_A, product_a)
        order_b = self._order_of(PRODUCER_B, _product(PRODUCER_B, "Tomate"))
        order_b.checkout_group_id = order_a.checkout_group_id

        session = self._producer_session(order_a, owns=True)
        svc = self._producer_service(session, PRODUCER_A)
        result = run(
            svc.confirm_delivery_and_payment(
                producer_phone="+22670000001", order_id=str(order_a.id)
            )
        )

        assert result["outcome"] == "COMPLETED"
        assert order_a.status == "COMPLETED"
        assert order_b.status == "CONFIRMED", "B ne doit JAMAIS suivre A"
        assert order_b.payment_status == "PENDING"

    def test_cancelling_one_order_never_touches_the_sibling(self):
        product_a = _product(PRODUCER_A, "Riz", qty=100.0)
        order_a = self._order_of(PRODUCER_A, product_a)
        product_b = _product(PRODUCER_B, "Tomate", qty=100.0)
        order_b = self._order_of(PRODUCER_B, product_b)
        order_b.checkout_group_id = order_a.checkout_group_id

        session = self._producer_session(order_a, owns=True)
        svc = self._producer_service(session, PRODUCER_A)
        run(
            svc.cancel_confirmed_order(
                producer_phone="+22670000001", order_id=str(order_a.id)
            )
        )

        assert order_a.status == "CANCELLED"
        assert order_b.status == "CONFIRMED"
        # Stock : seul le produit de A est recrédité
        assert product_a.quantity_for_sale == 102.0
        assert product_b.quantity_for_sale == 100.0


class TestAuctionOrdersAreUnaffected:
    def test_select_winning_bid_never_creates_order_items(self):
        """Preuve structurelle : les commandes d'enchère sont
        mono-producteur PAR CONSTRUCTION (aucun `OrderItem`, un seul
        `winning_bid_id`) — elles ne passent jamais par le split."""
        import inspect

        from ladini.services.database import auction as auction_mod

        source = inspect.getsource(auction_mod)
        assert "OrderItem(" not in source
        assert "winning_bid_id=bid.id" in source

    def test_auction_orders_have_no_checkout_group(self):
        import inspect

        from ladini.services.database.auction import AuctionMixin

        source = inspect.getsource(AuctionMixin.select_winning_bid)
        assert "checkout_group_id" not in source
