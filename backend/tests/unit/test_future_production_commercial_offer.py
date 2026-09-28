"""PRODUCTION_DECLARE_FUTURE -> `CommercialOffer` -> `MarketOffer` (Phase B2c.3).

Deux couches, sans DB réelle :
1. le moteur de parsing/validation (`evaluate_sales_offer`, RÉUTILISÉ tel quel — aucune 2e fabrique)
   pour les scénarios dorés A-E ;
2. `domain/agro.py::AgronomyService.declare_crop_cycle`, qui dérive le payload d'exécution de l'offre
   certifiée (jamais des champs plats), refuse PER_PACKAGE (non supporté pour MarketOffer), et
   retombe sur le comportement legacy quand aucune offre n'est fournie.
"""
from __future__ import annotations

import pytest

from ladini.domain.commercial_offer_flow import evaluate_sales_offer
from ladini.graphs.agents.market_coach.domain.agro import AgronomyService
from ladini.graphs.agents.market_coach.domain.model import DomainContext


def _offer(payload, *, said, text, question=None):
    return evaluate_sales_offer(payload, said=said, text=text, question=question)


BASE_PAYLOAD = {"product": "tomates", "quantity": 10.0, "unit": "TONNE"}


class TestGoldenAPerBaseUnit:
    def test_450000_or_400000_la_tonne_is_resolved_per_base_unit(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 400000.0, "price_unit": "TONNE"},
            said={"quantity": 10.0, "unit": "TONNE", "price": 400000.0, "price_unit": "TONNE"},
            text="10 tonnes de tomates à 400000 la tonne",
        )
        assert result.validation.is_valid
        assert result.offer.pricing.basis.value == "PER_BASE_UNIT"
        assert result.offer.pricing.basis_unit == "TONNE"


class TestGoldenBTotalLot:
    def test_pour_4_millions_au_total_is_resolved_total_lot(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 4_000_000.0},
            said={"quantity": 10.0, "unit": "TONNE", "price": 4_000_000.0},
            text="10 tonnes de tomates pour 4 millions au total",
        )
        assert result.validation.is_valid
        assert result.offer.pricing.basis.value == "TOTAL_LOT"
        assert result.offer.pricing.amount == 4_000_000.0


class TestGoldenCAmbiguousPriceAsksNeverGuesses:
    def test_a_bare_amount_is_incomplete_and_asks_the_precise_question(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 4_000_000.0},
            said={"quantity": 10.0, "unit": "TONNE", "price": 4_000_000.0},
            text="10 tonnes de tomates à 4000000",
        )
        assert result.validation.status == "INCOMPLETE"
        assert "price_basis" in result.validation.missing_fields
        assert result.question is not None and result.question.requested_field == "price_basis"
        assert "par tonne" in result.question_text and "ensemble" in result.question_text


class TestGoldenDQuestionContextNeverDependsOnTheLLM:
    def test_a_bare_reply_to_price_per_tonne_question_is_question_context_explicit(self):
        from ladini.domain.commercial_offer_flow import price_question

        question, _text = price_question("TONNE")
        result = _offer(
            {**BASE_PAYLOAD, "price": 400000.0},
            said={"price": 400000.0},
            text="400000",
            question=question,
        )
        assert result.validation.is_valid
        assert result.offer.pricing.basis.value == "PER_BASE_UNIT"
        assert result.offer.pricing.basis_source.value == "QUESTION_CONTEXT_EXPLICIT"


class TestGoldenECorrectionChangesTheBasis:
    def test_finalement_3_5_millions_pour_tout_switches_from_per_base_unit_to_total_lot(self):
        first = _offer(
            {**BASE_PAYLOAD, "price": 400000.0, "price_unit": "TONNE"},
            said={"quantity": 10.0, "unit": "TONNE", "price": 400000.0, "price_unit": "TONNE"},
            text="10 tonnes de tomates à 400000 la tonne",
        )
        assert first.offer.pricing.basis.value == "PER_BASE_UNIT"
        second = _offer(
            {**BASE_PAYLOAD, "price": 3_500_000.0, "commercial_offer": first.offer.to_dict()},
            said={"price": 3_500_000.0},
            text="finalement 3,5 millions pour tout",
        )
        assert second.validation.is_valid
        assert second.offer.pricing.basis.value == "TOTAL_LOT"
        assert second.offer.pricing.amount == 3_500_000.0


class TestProductChangePurgesTheOldOffer:
    def test_changing_the_product_drops_the_previous_pricing(self):
        first = _offer(
            {**BASE_PAYLOAD, "price": 400000.0, "price_unit": "TONNE"},
            said={"quantity": 10.0, "unit": "TONNE", "price": 400000.0, "price_unit": "TONNE"},
            text="10 tonnes de tomates à 400000 la tonne",
        )
        second = _offer(
            {"product": "pommes de terre", "commercial_offer": first.offer.to_dict()},
            said={"product": "pommes de terre"},
            text="finalement ce sera des pommes de terre",
        )
        assert second.offer.product == "pommes de terre"
        assert second.offer.pricing is None  # rien de l'ancienne offre ne survit


# =====================================================================
# `AgronomyService.declare_crop_cycle` — dérivation depuis l'offre certifiée
# =====================================================================


def _ctx():
    return DomainContext.from_state({"user_phone": "+22670000001"})


def _future_payload(**over):
    base = {
        "farm_id": "farm-1",
        "production_type": "CROP",
        "product": "tomates",
        "estimated_available_at": "2030-06-01",
    }
    base.update(over)
    return base


class TestDeclareCropCycleDerivesFromTheCertifiedOffer:
    def test_per_base_unit_offer_derives_the_normalized_price_and_carries_the_offer(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 400000.0, "price_unit": "TONNE"},
            said={"quantity": 10.0, "unit": "TONNE", "price": 400000.0, "price_unit": "TONNE"},
            text="10 tonnes de tomates à 400000 la tonne",
        )
        assert result.validation.is_valid
        payload = _future_payload(commercial_offer=result.offer.to_dict())
        svc = AgronomyService(context=_ctx())
        out = svc.declare_crop_cycle({"user_phone": "+22670000001"}, payload)
        args = out.tool_args["payload"]
        # inventaire en unité de base (TONNE -> KG) ; prix normalisé PAR KG, jamais 400000 tel quel.
        assert args["unit"] == "KG"
        assert args["quantity"] == pytest.approx(10000.0)
        assert args["price_per_unit"] == pytest.approx(400.0)
        assert args["commercial_offer"]["pricing"]["basis"] == "PER_BASE_UNIT"

    def test_total_lot_offer_never_multiplies_the_lot_amount_by_the_quantity(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 4_000_000.0},
            said={"quantity": 10.0, "unit": "TONNE", "price": 4_000_000.0},
            text="10 tonnes de tomates pour 4 millions au total",
        )
        payload = _future_payload(commercial_offer=result.offer.to_dict())
        svc = AgronomyService(context=_ctx())
        out = svc.declare_crop_cycle({"user_phone": "+22670000001"}, payload)
        args = out.tool_args["payload"]
        # prix normalisé dérivé (400 FCFA/KG) — le TOTAL du lot (4M) reste porté par l'offre certifiée,
        # relisible via `commercial_offer`, jamais recalculé comme `price_per_unit x quantity`.
        assert args["price_per_unit"] == pytest.approx(400.0)
        assert args["commercial_offer"]["pricing"]["basis"] == "TOTAL_LOT"
        assert args["commercial_offer"]["pricing"]["amount"] == 4_000_000.0

    def test_per_package_is_refused_fail_closed_market_offer_has_no_package_support(self):
        from ladini.domain.commercial_offer import (
            CommercialOffer,
            CommercialQuantity,
            InventoryQuantity,
            PackageDefinition,
            PackageStatus,
            PriceBasis,
            Pricing,
            Provenance,
        )

        offer = CommercialOffer(
            product="tomates",
            commercial_quantity=CommercialQuantity(200.0, "KG", Provenance.USER_EXPLICIT),
            inventory_quantity=InventoryQuantity(200.0, "KG", Provenance.USER_EXPLICIT),
            pricing=Pricing(
                amount=12000.0, basis=PriceBasis.PER_PACKAGE,
                source=Provenance.USER_EXPLICIT, basis_source=Provenance.USER_EXPLICIT,
            ),
            package=PackageDefinition(
                package_type="CAISSE", content_amount=25.0, content_unit="KG",
                status=PackageStatus.KNOWN, source=Provenance.USER_EXPLICIT,
            ),
        )
        assert offer.validate().is_valid  # l'offre elle-même est parfaitement valide...
        payload = _future_payload(commercial_offer=offer.to_dict())
        svc = AgronomyService(context=_ctx())
        with pytest.raises(ValueError, match="conditionnement"):
            svc.declare_crop_cycle({"user_phone": "+22670000001"}, payload)

    def test_an_invalid_offer_is_never_executed(self):
        incomplete = _offer(
            {**BASE_PAYLOAD, "price": 4_000_000.0},
            said={"quantity": 10.0, "unit": "TONNE", "price": 4_000_000.0},
            text="10 tonnes de tomates a 4000000",
        )
        assert not incomplete.validation.is_valid
        payload = _future_payload(commercial_offer=incomplete.offer.to_dict())
        svc = AgronomyService(context=_ctx())
        with pytest.raises(ValueError):
            svc.declare_crop_cycle({"user_phone": "+22670000001"}, payload)

    def test_legacy_fallback_without_a_commercial_offer_keeps_the_pre_b2c3_behavior(self):
        payload = _future_payload(quantity=10.0, unit="TONNE", price=400000.0)
        svc = AgronomyService(context=_ctx())
        out = svc.declare_crop_cycle({"user_phone": "+22670000001"}, payload)
        args = out.tool_args["payload"]
        assert args["unit"] == "KG"
        assert args["quantity"] == pytest.approx(10000.0)
        assert args["price_per_unit"] == pytest.approx(400.0)
        assert args.get("commercial_offer") is None
