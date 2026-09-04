"""`BuyerMixin.create_preorder_draft` — revalidation SERVEUR du seuil minimum
de commande (`governance.sub_categories`, 2026-09-04, clôture frontière
CART→CHECKOUT).

## Le gap réel fermé par ce test

AVANT ce correctif, le seuil minimum de commande n'était vérifié qu'à
l'AJOUT au panier (`cart_service.py::add_to_cart_with_ref`) — jamais relu au
moment où `create_preorder_draft` construit le snapshot serveur faisant foi.
Un panier composé AVANT qu'un admin ne relève le seuil pour ce type de
produit (`SubCategory.minimum_order_quantity`) pouvait ainsi produire une
précommande en dessous du minimum EN VIGUEUR, sans qu'aucune couche ne s'en
aperçoive — la classe même de divergence "cart valide, checkout valide,
règle actuelle invalide" que la clôture de cette frontière doit éliminer.

Réutilise la même fonction pure (`validate_minimum_order_quantity`) et la
même source canonique (`SubCategory`) que la référence déjà correcte
`finalize_multi_order` (jamais câblée dans le vrai chemin de checkout) —
aucune nouvelle règle inventée.

## Portée du faux moteur

Même limite qu'ailleurs dans ce dépôt (pas de Postgres de test) : un faux
moteur minimal qui répond à `session.scalar` (Product) et `session.execute`
(la requête `SubCategory.minimum_order_quantity/unit`) avec des valeurs
préparées — suffisant pour prouver que la RÈGLE est appliquée et que
l'article est écarté proprement, pas que la transaction SQL réelle l'est."""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run

from agriconnect.services.database.buyer import BuyerMixin
from agriconnect.services.database.errors import BusinessRuleException


def _fake_buyer_profile():
    user = types.SimpleNamespace(id=uuid.uuid4(), name="Acheteur Test", zone_id=None)
    profile = types.SimpleNamespace(id=uuid.uuid4(), establishment_name=None)
    return user, profile


async def _async_return(value):
    return value


class _FakeSession:
    """Capture les `OrderItem` réellement construits ; répond à `.scalar`
    (Product) et `.execute` (requête SubCategory) — voir portée en tête de
    fichier."""

    def __init__(self, product, sub_category_row=None):
        self._product = product
        self._sub_category_row = sub_category_row
        self.added: list = []

    async def scalar(self, _stmt):
        return self._product

    async def execute(self, _stmt):
        row = self._sub_category_row
        return types.SimpleNamespace(first=lambda: row)

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


def _flat_product(**overrides):
    base = {
        "id": uuid.uuid4(),
        "name": "Tomates",
        "price": 250.0,
        "unit": "KG",
        "quantity_for_sale": 1000.0,
        "producer_id": uuid.uuid4(),
        "sub_category_id": uuid.uuid4(),
        "pricing_tiers": None,
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _tiered_product(**overrides):
    base = {
        "id": uuid.uuid4(),
        "name": "Lait local",
        "price": 900.0,
        "unit": "LITRE",
        "quantity_for_sale": 1000.0,
        "producer_id": uuid.uuid4(),
        "sub_category_id": uuid.uuid4(),
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


class TestFlatProductMinimumOrderRevalidatedServerSide:
    def test_below_the_current_platform_minimum_is_rejected_not_silently_billed(
        self, monkeypatch
    ):
        """Le panier a été composé pour 50 KG, mais le seuil PLATEFORME
        (`SubCategory`) est désormais 100 KG (relevé après l'ajout au
        panier) — la précommande ne doit PAS se créer avec cet article en
        dessous du seuil EN VIGUEUR."""
        product = _flat_product()
        session = _FakeSession(product, sub_category_row=(100.0, "KG"))
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(
                svc.create_preorder_draft(
                    buyer_phone="+22670000001",
                    cart_items=[{"product_id": str(product.id), "quantity": 50}],
                )
            )
        assert exc_info.value.reason == "no_valid_items"
        # Rien n'a été écrit — le seul article du panier a été écarté.
        assert not [o for o in session.added if type(o).__name__ == "OrderItem"]

    def test_above_or_equal_the_minimum_is_accepted_unchanged(self, monkeypatch):
        product = _flat_product()
        session = _FakeSession(product, sub_category_row=(50.0, "KG"))
        svc = _service(session)

        result = run(
            svc.create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[{"product_id": str(product.id), "quantity": 50}],
            )
        )
        assert result["status"] == "success"
        assert result["total_amount"] == 12500.0
        assert result.get("unresolved_items") == []

    def test_no_rule_configured_leaves_behaviour_untouched(self, monkeypatch):
        product = _flat_product()
        session = _FakeSession(product, sub_category_row=(None, None))
        svc = _service(session)

        result = run(
            svc.create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[{"product_id": str(product.id), "quantity": 1}],
            )
        )
        assert result["status"] == "success"


class TestTieredCartItemMinimumOrderUsesBaseUnitQuantityNeverPackageCount:
    def test_package_count_below_minimum_in_base_unit_is_rejected(self, monkeypatch):
        """3 bidons de 10L = 30L de base — le seuil plateforme est fixé à
        50L : DOIT être comparé à 30 (la quantité RÉELLE), jamais à 3 (le
        nombre de paquets, règle "pas de tetris" — voir domain/pricing_tiers.py)."""
        product = _tiered_product()
        session = _FakeSession(product, sub_category_row=(50.0, "L"))
        svc = _service(session)

        with pytest.raises(BusinessRuleException) as exc_info:
            run(
                svc.create_preorder_draft(
                    buyer_phone="+22670000001",
                    cart_items=[
                        {
                            "product_id": str(product.id),
                            "quantity": 3,
                            "tier_id": "t-10L",
                        }
                    ],
                )
            )
        assert exc_info.value.reason == "no_valid_items"

    def test_package_count_meeting_minimum_in_base_unit_is_accepted(self, monkeypatch):
        product = _tiered_product()
        session = _FakeSession(product, sub_category_row=(30.0, "L"))
        svc = _service(session)

        result = run(
            svc.create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[
                    {"product_id": str(product.id), "quantity": 3, "tier_id": "t-10L"}
                ],
            )
        )
        assert result["status"] == "success"
        assert result["items"][0]["base_unit_quantity"] == 30.0


class TestMixedCartOnlyTheFailingItemIsDropped:
    def test_one_item_below_minimum_does_not_block_the_rest_of_the_cart(self, monkeypatch):
        good = _flat_product(name="Maïs")
        bad = _flat_product(name="Riz")

        class _MultiSession(_FakeSession):
            async def scalar(self, _stmt):
                # Un seul type de requête Product par item, dans l'ordre du
                # panier — répond séquentiellement.
                return self._queue.pop(0)

            async def execute(self, _stmt):
                return types.SimpleNamespace(first=lambda: (100.0, "KG"))

        session = _MultiSession(good)
        session._queue = [good, bad]  # good d'abord, bad ensuite

        svc = _service(session)
        result = run(
            svc.create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[
                    {"product_id": str(good.id), "quantity": 200},  # au-dessus du seuil
                    {"product_id": str(bad.id), "quantity": 10},  # en dessous du seuil
                ],
            )
        )
        assert result["status"] == "success"
        assert result["items_count"] == 1
        assert result["items"][0]["name"] == "Maïs"
        assert result["unresolved_items"] == [str(bad.id)]
