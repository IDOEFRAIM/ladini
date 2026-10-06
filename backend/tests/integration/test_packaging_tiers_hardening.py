"""Hardening PR-readiness de la tarification par conditionnements (2026-09-29).

- AMBIGUOUS_PRICING : un palier lisible + un palier illisible -> clarification ciblée qui DIT ce qui est compris ;
- 1 palier SANS mot de conditionnement -> PACKAGING_TIERS de bout en bout, sans « bidon » inventé ;
- invariant legacy shadow : le chemin acheteur critique n'utilise JAMAIS `Product.price` ;
- update_product_price_and_qty : une édition de stock ne détruit pas les paliers.
"""
from __future__ import annotations

import types
import uuid

import pytest

from tests.conftest import run
from tests.integration.test_sales_publish_packaging_tiers import (  # noqa: F401  (fixture `conv`)
    B5,
    B9,
    _ans,
    _creates,
    _draft,
    _miel_60,
    conv,
)
from tests.unit.test_create_preorder_draft_pricing_tiers import (
    _FakeSession,
    _service,
    _tiered_product,
)

pytestmark = pytest.mark.integration


class TestAmbiguousPricing:
    def test_one_readable_tier_and_one_unreadable_keeps_the_tunnel_and_says_what_was_understood(self, conv, caplog):
        import logging

        _miel_60(conv)
        half = [B5, {"quantity": 9.0, "unit": "L", "price": None, "packaging": "bidon"}]
        with caplog.at_level(logging.INFO):
            t = conv.send("bidon de 5 L à 700 et je sais pas pour l'autre", llm=_ans(pricing_tiers=half))
        assert "Je n'ai pas bien saisi" not in t.response
        assert "J'ai bien compris" in t.response and "Bidon de 5 L : 700 FCFA" in t.response
        assert "contenance ou le prix" in t.response
        assert "reason=AMBIGUOUS_PRICING" in caplog.text
        assert t.pending_after.kind.value == "ENTER_FIELD" and t.pending_after.field == "price"
        assert not _draft(conv) and not _creates(conv), "rien n'est publié tant que le palier est incomplet"

    def test_the_user_can_then_restate_both_tiers(self, conv):
        _miel_60(conv)
        half = [B5, {"quantity": 9.0, "unit": "L", "price": None, "packaging": "bidon"}]
        conv.send("bidon de 5 L à 700 et je sais pas pour l'autre", llm=_ans(pricing_tiers=half))
        t = conv.send("bidon de 5 L à 700 et bidon de 9 L à 1000", llm=_ans(pricing_tiers=[B5, B9]))
        assert "Confirmez-vous" in t.response and len(_draft(conv)["pricing_tiers"]) == 2


class TestSingleTierWithoutPackagingWord:
    def _unnamed(self):
        return [{**B5, "packaging": None}]

    def test_confirmation_and_persistence_never_invent_a_packaging(self, conv):
        _miel_60(conv)
        t = conv.send("5 L à 700", llm=_ans(pricing_tiers=self._unnamed()))
        assert "Confirmez-vous" in t.response
        assert "- 5 L : 700 FCFA" in t.response and "bidon" not in t.response.lower()
        d = _draft(conv)
        assert d["commercial_offer"] is None and d["price"] is None and d["quantity"] == 60.0
        conv.send("oui")
        (call,) = _creates(conv)
        assert call["quantity_for_sale"] == 60.0 and call["price"] == 700.0  # 700 = legacy shadow uniquement
        assert call["pricing_tiers"] == [{"quantity": 5.0, "unit": "L", "price": 700.0, "packaging": None}]
        assert "commercial_offer" not in call

    def test_buyer_can_order_it_and_the_stock_debit_is_the_content_not_the_count(self):
        from ladini.domain.pricing_tiers import (
            resolve_stock_debit,
            tiers_to_dicts,
            validate_pricing_tiers,
        )

        tiers = tiers_to_dicts(validate_pricing_tiers(self._unnamed(), "LITRE"))
        product = _tiered_product(name="miel", price=700.0, quantity_for_sale=60.0, pricing_tiers=tiers)
        session = _FakeSession(product)
        result = run(
            _service(session).create_preorder_draft(
                buyer_phone="+22670000001",
                cart_items=[{"product_id": str(product.id), "quantity": 3, "tier_id": tiers[0]["tier_id"]}],
            )
        )
        assert result["total_amount"] == 2100.0
        (item,) = [o for o in session.added if type(o).__name__ == "OrderItem"]
        assert item.base_unit_quantity == 15.0 and resolve_stock_debit(item) == 15.0

    def test_buyer_search_label_shows_the_tier_only(self):
        from ladini.domain.commercial_pricing_snapshot import product_pricing_view

        view = product_pricing_view({"price": 700.0, "pricing_tiers": self._unnamed(), "commercial_pricing": None})
        assert view.pricing_label == "5 L : 700 FCFA"


class TestLegacyShadowInvariant:
    """Le test « 2 x 9 L » existe déjà : `test_sales_publish_packaging_tiers.py::
    TestK_PublishedTiersAreWhatTheBuyerPays` (publication réelle -> préparation de commande, 2000 / 18 L /
    reste 42 L). Ici : le MÊME scénario, mais le shadow est rendu PIÈGE (0.01) pour prouver qu'il n'entre dans
    aucun calcul de montant ni de débit."""

    def test_price_and_stock_are_computed_from_the_tier_even_if_the_shadow_is_a_trap(self):
        from ladini.domain.pricing_tiers import resolve_stock_debit

        tiers = [
            {"tier_id": "t5", "quantity": 5.0, "unit": "L", "price": 700.0, "packaging": "bidon",
             "base_unit_quantity": 5.0, "min_order_quantity": 1},
            {"tier_id": "t9", "quantity": 9.0, "unit": "L", "price": 1000.0, "packaging": "bidon",
             "base_unit_quantity": 9.0, "min_order_quantity": 1},
        ]
        for shadow in (700.0, 0.01, 999999.0):
            product = _tiered_product(name="miel", price=shadow, quantity_for_sale=60.0, pricing_tiers=tiers)
            session = _FakeSession(product)
            result = run(
                _service(session).create_preorder_draft(
                    buyer_phone="+22670000001",
                    cart_items=[{"product_id": str(product.id), "quantity": 2, "tier_id": "t9"}],
                )
            )
            assert result["total_amount"] == 2000.0, shadow
            (item,) = [o for o in session.added if type(o).__name__ == "OrderItem"]
            assert resolve_stock_debit(item) == 18.0 and 60.0 - resolve_stock_debit(item) == 42.0


class TestUpdateProductDoesNotDestroyTiers:
    def _update(self, product, **fields):
        from ladini.services.database.product import ProductMixin

        class _Res:
            def scalar_one_or_none(self):
                return product

        class _Sess:
            async def execute(self, stmt):
                return _Res()

            async def flush(self):
                pass

            async def refresh(self, obj):
                pass

        class _Svc(ProductMixin):
            @property
            def session(self):
                return _Sess()

        svc = _Svc()

        async def _profile(phone):
            return None, types.SimpleNamespace(id=uuid.uuid4())

        svc.get_producer_profile = _profile
        return run(svc.update_product_price_and_qty("+22670000001", str(uuid.uuid4()), **fields))

    def _product(self):
        return types.SimpleNamespace(
            id=uuid.uuid4(), name="miel", price=700.0, unit="LITRE", quantity_for_sale=60.0, is_available=True,
            pricing_tiers=[dict(B5, tier_id="t5", base_unit_quantity=5.0, min_order_quantity=1)],
            commercial_pricing=None, to_dict=lambda: {},
        )

    def test_a_stock_only_edit_leaves_the_tiers_untouched(self, monkeypatch):
        from ladini.services.database import product as product_mod

        class _Emitter:
            def __init__(self, *_a, **_k):
                pass

            async def emit_product_quantity_changed(self, *a, **k):
                pass

            async def emit_product_published_for_sale(self, *a, **k):
                pass

        monkeypatch.setattr(product_mod, "BusinessEventEmitter", _Emitter)
        product = self._product()
        before = list(product.pricing_tiers)
        res = self._update(product, quantity=40.0)
        assert res["status"] == "success", res
        assert product.quantity_for_sale == 40.0
        assert product.pricing_tiers == before

    def test_a_legacy_scalar_price_edit_changes_the_shadow_only_never_the_tiers(self, monkeypatch):
        """Limite documentée : sur un produit à paliers, « prix 800 » ne modifie que le shadow legacy (aucun
        effet commercial) — les paliers restent la vérité. Corriger un tarif passe par `pricing_tiers`."""
        from ladini.services.database import product as product_mod

        class _Emitter:
            def __init__(self, *_a, **_k):
                pass

            async def emit_product_quantity_changed(self, *a, **k):
                pass

            async def emit_product_published_for_sale(self, *a, **k):
                pass

        monkeypatch.setattr(product_mod, "BusinessEventEmitter", _Emitter)
        product = self._product()
        before = list(product.pricing_tiers)
        self._update(product, price=800.0)
        assert product.pricing_tiers == before
