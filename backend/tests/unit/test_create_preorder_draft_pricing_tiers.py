"""`BuyerMixin.create_preorder_draft` — résolution SERVEUR des paliers de
prix/conditionnement (2026-09-04, audit CART→CHECKOUT).

## Le gap réel fermé par ce test

AVANT ce correctif, `create_preorder_draft` traitait TOUT article de panier
comme un produit à tarif unique (`product.price * quantity`) — pour un
article à PALIER (`tier_id` posé par `cart_service.py::add_to_cart_with_ref`),
`quantity` est un NOMBRE DE PAQUETS, pas une quantité en unité de base :
la commande finale sous-facturait ET sous-débitait le stock d'un facteur
égal à la taille du palier (ex: 3 bidons de 10L facturés/débités comme 3 L
au lieu de 3 bidons à leur propre tarif — 30L réellement dus). Ce fichier
verrouille que `resolve_tier`/`compute_line` (déjà éprouvés,
`tests/unit/test_pricing_tiers.py`) sont désormais RÉELLEMENT appelés côté
serveur, jamais confiance au panier client au-delà du `tier_id`.

## Portée du faux moteur

Comme partout ailleurs dans ce dépôt (aucune infrastructure Postgres de
test) : un faux moteur MINIMAL, qui capture les objets `OrderItem`
réellement construits (`session.add(...)`) plutôt que de simuler le SQL —
suffisant pour prouver que les CHAMPS écrits sont corrects, pas que la
transaction elle-même est valide contre un vrai Postgres."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from ladini.services.database.buyer import BuyerMixin
from ladini.services.database.errors import BusinessRuleException


def _fake_buyer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4(), name="Acheteur Test", zone_id=None)
    profile = types.SimpleNamespace(id=uuid.uuid4(), establishment_name=None)
    return user, profile


async def _async_return(value):
    return value


class _FakeSession:
    """Capture chaque objet `session.add(...)` — `Order` ET `OrderItem` —
    ne simule AUCUNE sémantique SQL réelle (voir portée en tête de fichier)."""

    def __init__(self, product):
        self._product = product
        self.added: list = []

    async def scalar(self, _stmt):
        # Un seul type de requête dans `create_preorder_draft` avant l'INSERT
        # de l'`Order` (get_buyer_profile est monkeypatché séparément) :
        # `select(Product).where(Product.id == p_uuid)`.
        return self._product

    def add(self, obj):
        self.added.append(obj)

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


def _tiered_product(**overrides):
    base = {
        "id": uuid.uuid4(),
        "name": "Lait local",
        "price": 900.0,  # tarif "1er palier"/compat historique — PAS ce qui doit être facturé ici
        "unit": "LITRE",
        "quantity_for_sale": 1000.0,
        "producer_id": uuid.uuid4(),
        "pricing_tiers": [
            {
                "tier_id": "t-10L",
                "quantity": 10.0,
                "unit": "L",
                "price": 5000.0,
                "packaging": "bidon",
                "base_unit_quantity": 10.0,
                "min_order_quantity": 1,
            }
        ],
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


class TestTieredCartItemIsCorrectlyResolvedServerSide:
    def test_package_count_is_never_billed_or_debited_as_base_quantity(self, monkeypatch):
        """3 bidons de 10L à 5000 FCFA/bidon — DOIT facturer 15000 FCFA
        (jamais 900*3=2700, le tarif du produit "de base") et débiter 30L
        de stock côté logique (jamais 3)."""
        product = _tiered_product()
        session = _FakeSession(product)
        svc = _service(session)

        result = run(
            svc.create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[
                    {
                        "product_id": str(product.id),
                        "quantity": 3,  # NOMBRE DE PAQUETS, pas des litres
                        "tier_id": "t-10L",
                    }
                ],
            )
        )

        assert result["status"] == "success"
        # Facturation : 3 paquets * 5000 FCFA/paquet = 15000, JAMAIS 3*900.
        assert result["total_amount"] == 15000.0
        assert result["subtotal"] == 15000.0

        order_items = [o for o in session.added if type(o).__name__ == "OrderItem"]
        assert len(order_items) == 1
        item = order_items[0]
        assert item.tier_id == "t-10L"
        # base_unit_quantity = CE que confirm_preorder_draft doit débiter du
        # stock (via resolve_stock_debit) — 3 paquets * 10L = 30L, jamais 3.
        assert item.base_unit_quantity == 30.0
        assert item.price_at_sale == 5000.0
        assert item.quantity == 3  # nombre de paquets, préservé tel quel

        resolved = result["items"][0]
        assert resolved["base_unit_quantity"] == 30.0
        assert resolved["price"] == 5000.0
        assert resolved["line_total"] == 15000.0
        assert resolved["tier_id"] == "t-10L"

    def test_a_stale_tier_id_no_longer_present_on_the_product_is_skipped_not_crashed(
        self, monkeypatch
    ):
        """Le catalogue a pu changer ses paliers entre l'ajout au panier et
        le checkout — un `tier_id` périmé ne doit JAMAIS faire planter toute
        la précommande (les autres articles valides doivent survivre), mais
        ne doit pas non plus être silencieusement mal-facturé."""
        product = _tiered_product(pricing_tiers=[])  # palier retiré entre-temps
        session = _FakeSession(product)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(
                svc.create_preorder_draft(
                    buyer_phone="+22670000001",
                    cart_items=[
                        {"product_id": str(product.id), "quantity": 3, "tier_id": "t-10L"}
                    ],
                )
            )
        assert exc_info.value.reason == "no_valid_items"

    def test_non_integer_package_count_is_rejected_not_truncated(self, monkeypatch):
        """Même garde que côté panier (`add_to_cart_with_ref`, Faille B) —
        un panier corrompu/rejoué avec un nombre de paquets non entier ne
        doit jamais être tronqué silencieusement côté serveur non plus."""
        product = _tiered_product()
        session = _FakeSession(product)
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(
                svc.create_preorder_draft(
                    buyer_phone="+22670000001",
                    cart_items=[
                        {"product_id": str(product.id), "quantity": 2.5, "tier_id": "t-10L"}
                    ],
                )
            )
        assert exc_info.value.reason == "no_valid_items"


class TestNonTieredCartItemIsUnaffected:
    def test_flat_priced_product_still_uses_product_price_directly(self, monkeypatch):
        product = _tiered_product(pricing_tiers=None, price=250.0, unit="KG")
        session = _FakeSession(product)
        svc = _service(session)

        result = run(
            svc.create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[{"product_id": str(product.id), "quantity": 50}],
            )
        )
        assert result["total_amount"] == 12500.0
        order_items = [o for o in session.added if type(o).__name__ == "OrderItem"]
        assert order_items[0].tier_id is None
        assert order_items[0].base_unit_quantity is None
