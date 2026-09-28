"""`declare_future_production` -> `MarketOffer.pricing_snapshot` (Phase B2c.3).

Même doublure de session que `test_declare_future_production_ownership.py` (pas de Postgres réel) :
prouve que `certify_commercial_offer` (déjà verrouillé côté `create_product`, réutilisé ici SANS
nouvelle fabrique) dérive et écrit le snapshot, rejette AVANT toute écriture une offre dont les
champs legacy divergent, et laisse `pricing_snapshot=NULL` quand aucune offre n'est transmise."""
from __future__ import annotations

import types
import uuid

import pytest

from ladini.domain.commercial_offer import (
    CommercialOffer,
    CommercialQuantity,
    InventoryQuantity,
    PriceBasis,
    Pricing,
    Provenance,
)
from ladini.services.database.errors import BusinessRuleException
from ladini.services.database.producer import ProducerMgmtMixin
from tests.conftest import run


class _ReachedWrite(Exception):
    def __init__(self, offer):
        self.offer = offer


class _FakeSession:
    def __init__(self, farm):
        self._farm = farm
        self.added: list = []

    async def get(self, model, pk):
        return self._farm

    def add(self, obj):
        self.added.append(obj)
        raise _ReachedWrite(obj)

    async def flush(self):  # pragma: no cover - add() already raises
        pass

    async def refresh(self, obj):  # pragma: no cover
        pass


def _service(session, *, user_row):
    class _Svc(ProducerMgmtMixin):
        @property
        def session(self):
            return session

        async def _resolve_producer_phone(self, *, phone=None, producer_id=None):
            return phone or "+22670000001"

        async def _fetch_user_entities(self, phone):
            return user_row

    return _Svc()


def _setup():
    me = types.SimpleNamespace(id=uuid.uuid4())
    farm = types.SimpleNamespace(id=uuid.uuid4(), producer_id=me.id)
    session = _FakeSession(farm=farm)
    svc = _service(session, user_row=(object(), me))
    return svc, farm, session


def _offer(*, amount, basis, basis_unit=None, quantity=10.0, unit="KG"):
    return CommercialOffer(
        product="tomates",
        commercial_quantity=CommercialQuantity(quantity, unit, Provenance.USER_EXPLICIT),
        inventory_quantity=InventoryQuantity(quantity, unit, Provenance.USER_EXPLICIT),
        pricing=Pricing(
            amount=amount, basis=PriceBasis(basis), basis_unit=basis_unit,
            source=Provenance.USER_EXPLICIT, basis_source=Provenance.USER_EXPLICIT,
        ),
    )


class TestPricingSnapshotIsPersistedFromTheCertifiedOffer:
    def test_per_base_unit_offer_writes_a_certified_snapshot(self):
        svc, farm, session = _setup()
        offer = _offer(amount=400.0, basis="PER_BASE_UNIT", basis_unit="KG", quantity=10000.0)
        payload = {
            "farm_id": str(farm.id), "product_label": "tomates", "quantity": 10000, "unit": "KG",
            "price_per_unit": 400.0, "estimated_available_at": "2030-06-01",
            "commercial_offer": offer.to_dict(),
        }
        with pytest.raises(_ReachedWrite) as exc_info:
            run(svc.declare_future_production(payload, phone="+22670000001"))
        market_offer = exc_info.value.offer
        assert market_offer.pricing_snapshot is not None
        assert market_offer.pricing_snapshot["price_basis"] == "PER_BASE_UNIT"
        assert market_offer.pricing_snapshot["commercial_price_amount"] == "400"

    def test_total_lot_offer_writes_the_lot_amount_not_a_multiplied_total(self):
        svc, farm, session = _setup()
        offer = _offer(amount=4_000_000.0, basis="TOTAL_LOT", quantity=10000.0, unit="KG")
        payload = {
            "farm_id": str(farm.id), "product_label": "tomates", "quantity": 10000, "unit": "KG",
            "price_per_unit": 400.0, "estimated_available_at": "2030-06-01",
            "commercial_offer": offer.to_dict(),
        }
        with pytest.raises(_ReachedWrite) as exc_info:
            run(svc.declare_future_production(payload, phone="+22670000001"))
        snap = exc_info.value.offer.pricing_snapshot
        assert snap["price_basis"] == "TOTAL_LOT"
        assert snap["commercial_price_amount"] == "4000000"

    def test_no_commercial_offer_leaves_pricing_snapshot_null(self):
        svc, farm, session = _setup()
        payload = {
            "farm_id": str(farm.id), "product_label": "mil", "quantity": 5000, "unit": "KG",
            "price_per_unit": 300.0, "estimated_available_at": "2030-06-01",
        }
        with pytest.raises(_ReachedWrite) as exc_info:
            run(svc.declare_future_production(payload, phone="+22670000001"))
        assert exc_info.value.offer.pricing_snapshot is None


class TestDualWriteRejectsBeforeAnyWrite:
    def test_a_legacy_price_that_contradicts_the_certified_offer_is_rejected(self):
        svc, farm, session = _setup()
        offer = _offer(amount=400.0, basis="PER_BASE_UNIT", basis_unit="KG", quantity=10000.0)
        payload = {
            "farm_id": str(farm.id), "product_label": "tomates", "quantity": 10000, "unit": "KG",
            # `price_per_unit` volontairement FAUX (l'offre dit 400, pas 999) :
            "price_per_unit": 999.0, "estimated_available_at": "2030-06-01",
            "commercial_offer": offer.to_dict(),
        }
        with pytest.raises(BusinessRuleException, match="incohérente"):
            run(svc.declare_future_production(payload, phone="+22670000001"))
        assert not session.added  # AUCUNE écriture avant le rejet

    def test_a_quantity_that_contradicts_the_offers_inventory_is_rejected(self):
        svc, farm, session = _setup()
        offer = _offer(amount=400.0, basis="PER_BASE_UNIT", basis_unit="KG", quantity=10000.0, unit="KG")
        payload = {
            "farm_id": str(farm.id), "product_label": "tomates", "quantity": 1, "unit": "KG",
            "price_per_unit": 400.0, "estimated_available_at": "2030-06-01",
            "commercial_offer": offer.to_dict(),
        }
        with pytest.raises(BusinessRuleException, match="incohérente"):
            run(svc.declare_future_production(payload, phone="+22670000001"))
        assert not session.added
