"""`ProductMixin.toggle_product_availability` — Producer Analytics Phase B,
real data-corruption bug fixed here (docs/analytics/
PRODUCER_ANALYTICS_ARCHITECTURE.md §3.2/§30): the method used to fake ON/OFF
by writing `quantity_for_sale = 0.0`/`1.0`, permanently destroying the real
quantity on every pause->resume cycle (500 KG paused -> resumed -> 1 KG,
forever). Fixed to flip the pre-existing `is_available` boolean instead —
no migration needed, `is_available` already exists and is already the
column `BuyerMixin.search_products` filters on.

Mission tests A-F:
  A. 500 KG -> pause -> quantity_for_sale stays 500
  B. pause -> resume -> quantity_for_sale stays 500
  C. a product at 0 KG behaves consistently (toggle still just flips
     visibility, never touches the 0)
  D. buyer search excludes a paused product (already proven by the existing
     `is_available.is_(True)` filter — locked in by source inspection here)
  E. a resumed product becomes eligible again (`is_available` flips back)
  F. no regression to checkout/matching (they read `is_available` and
     `quantity_for_sale` independently; this fix does not touch either
     column's read side, only which one this ONE method writes)
"""
from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.conftest import run

_PRODUCT_ID = str(uuid.uuid4())


def _source() -> str:
    from ladini.services.database.product import ProductMixin

    return inspect.getsource(ProductMixin.toggle_product_availability)


class TestSourceNeverFakesQuantityAsABoolean:
    """Cheap, reliable regression lock — same style as
    `test_product_unpublish_capability.py::TestBusinessRuleStaysInTheDatabaseLayer`."""

    def test_never_assigns_quantity_for_sale_at_all(self):
        # The docstring legitimately explains the OLD bug by name — only the
        # executable body (after the closing `"""`) must never touch the column.
        source = _source()
        body = source.split('"""', 2)[-1]
        assert "quantity_for_sale" not in body

    def test_flips_is_available_instead(self):
        source = _source()
        assert "product.is_available = not was_available" in source

    def test_still_locked_by_row_level_update(self):
        source = _source()
        assert "with_for_update" in source


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


def _testable_product_mixin_class():
    """A private subclass, defined fresh per call, so patching `session` here
    never leaks onto the real `ProductMixin` class used elsewhere in the suite."""
    from ladini.services.database.product import ProductMixin

    class _TestableProductMixin(ProductMixin):
        def __init__(self, fake_session):
            self._fake_session = fake_session

        @property
        def session(self):
            return self._fake_session

    return _TestableProductMixin


class TestToggleBehaviorPreservesQuantity:
    def _make_mixin(self, product):
        session = SimpleNamespace(
            execute=AsyncMock(return_value=_FakeResult(product)),
            flush=AsyncMock(),
            refresh=AsyncMock(),
        )
        mixin = _testable_product_mixin_class()(session)
        mixin.get_producer_profile = AsyncMock(return_value=(None, SimpleNamespace(id="producer-1")))
        return mixin, session

    def test_pausing_a_500kg_product_leaves_the_quantity_at_500(self):
        product = SimpleNamespace(
            id=_PRODUCT_ID, producer_id="producer-1", is_available=True,
            quantity_for_sale=500.0, price=100, to_dict=lambda: {"quantity_for_sale": 500.0, "is_available": False},
        )
        mixin, _ = self._make_mixin(product)
        result = run(mixin.toggle_product_availability(phone="+22670000001", product_id=_PRODUCT_ID))
        assert result["status"] == "success"
        assert product.quantity_for_sale == 500.0  # untouched
        assert product.is_available is False
        assert result["active"] is False

    def test_resuming_after_pause_restores_full_eligibility_not_a_fake_1kg(self):
        product = SimpleNamespace(
            id=_PRODUCT_ID, producer_id="producer-1", is_available=False,
            quantity_for_sale=500.0, price=100, to_dict=lambda: {},
        )
        mixin, _ = self._make_mixin(product)
        result = run(mixin.toggle_product_availability(phone="+22670000001", product_id=_PRODUCT_ID))
        assert result["status"] == "success"
        assert product.quantity_for_sale == 500.0  # NEVER became 1.0
        assert product.is_available is True
        assert result["active"] is True

    def test_product_at_zero_quantity_toggles_visibility_only(self):
        product = SimpleNamespace(
            id=_PRODUCT_ID, producer_id="producer-1", is_available=True,
            quantity_for_sale=0.0, price=100, to_dict=lambda: {},
        )
        mixin, _ = self._make_mixin(product)
        result = run(mixin.toggle_product_availability(phone="+22670000001", product_id=_PRODUCT_ID))
        assert product.quantity_for_sale == 0.0
        assert product.is_available is False
        assert result["status"] == "success"

    def test_publish_event_fires_only_on_the_off_to_on_transition(self, monkeypatch):
        captured = []

        async def _fake_emit(self, product):
            captured.append(product.id)
            return True

        monkeypatch.setattr(
            "ladini.services.database.product.BusinessEventEmitter.emit_product_published_for_sale",
            _fake_emit,
        )
        product = SimpleNamespace(
            id=_PRODUCT_ID, producer_id="producer-1", is_available=False,
            quantity_for_sale=500.0, price=100, to_dict=lambda: {},
        )
        mixin, _ = self._make_mixin(product)
        run(mixin.toggle_product_availability(phone="+22670000001", product_id=_PRODUCT_ID))
        assert captured == [_PRODUCT_ID]

    def test_no_publish_event_on_the_on_to_off_transition(self, monkeypatch):
        called = AsyncMock()
        monkeypatch.setattr(
            "ladini.services.database.product.BusinessEventEmitter.emit_product_published_for_sale",
            called,
        )
        product = SimpleNamespace(
            id=_PRODUCT_ID, producer_id="producer-1", is_available=True,
            quantity_for_sale=500.0, price=100, to_dict=lambda: {},
        )
        mixin, _ = self._make_mixin(product)
        run(mixin.toggle_product_availability(phone="+22670000001", product_id=_PRODUCT_ID))
        called.assert_not_awaited()


class TestBuyerSearchStillExcludesPausedProducts:
    """Mission test D — proven the same way `test_product_unpublish_capability.py`
    already proves it (source inspection of the real compiled SQL filter),
    not re-derived here."""

    def test_search_filters_on_is_available(self):
        from ladini.services.database.buyer import BuyerMixin

        source = inspect.getsource(BuyerMixin.search_products)
        assert "Product.is_available.is_(True)" in source
