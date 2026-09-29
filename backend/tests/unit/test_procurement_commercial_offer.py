"""PROCUREMENT_CREATE_REQUEST -> `CommercialOffer` -> `ProcurementDraft` (Phase B2c.5).

Deux couches, sans DB reelle :
1. le moteur de parsing/validation (`evaluate_sales_offer`, REUTILISE tel quel - meme moteur que
   SALES_PUBLISH_PRODUCT/PRODUCTION_DECLARE_FUTURE, aucune 2e fabrique) pour les scenarios dores ;
2. `ProcurementDraft.render_summary`/`execution_payload`, qui derive le prix plafond de l'offre
   certifiee (jamais des champs plats bruts), refuse PER_PACKAGE (non supporte pour un appel
   d'offres), et retombe sur le comportement legacy quand aucune offre n'est fournie.

Avant cette phase, `reconcile_price_basis` (l'ancien mecanisme) n'avait aucune notion de
TOTAL_LOT : "10 tonnes de mais pour 4 millions au total" etait silencieusement stocke comme
4 000 000 FCFA/TONNE (un plafond 10x trop haut). C'est exactement le bug ferme ici.
"""
from __future__ import annotations

import pytest

from ladini.domain.commercial_offer_flow import evaluate_sales_offer
from ladini.graphs.agents.market_coach.domain.procurement_draft import ProcurementDraft

BASE_PAYLOAD = {"product": "mais", "quantity": 10.0, "unit": "TONNE"}


def _offer(payload, *, said, text, question=None):
    return evaluate_sales_offer(payload, said=said, text=text, question=question)


class TestGoldenAPerBaseUnit:
    def test_450000_la_tonne_is_resolved_per_base_unit(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 450000.0, "price_unit": "TONNE"},
            said={"quantity": 10.0, "unit": "TONNE", "price": 450000.0, "price_unit": "TONNE"},
            text="10 tonnes de mais a maximum 450000 la tonne",
        )
        assert result.validation.is_valid
        assert result.offer.pricing.basis.value == "PER_BASE_UNIT"


class TestGoldenBTotalBudget:
    def test_pour_4_millions_au_total_is_resolved_total_lot(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 4_000_000.0},
            said={"quantity": 10.0, "unit": "TONNE", "price": 4_000_000.0},
            text="10 tonnes de mais pour 4 millions au total",
        )
        assert result.validation.is_valid
        assert result.offer.pricing.basis.value == "TOTAL_LOT"
        assert result.offer.pricing.amount == 4_000_000.0


class TestGoldenCAmbiguous:
    def test_a_bare_amount_is_incomplete_and_asks_the_precise_question(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 4_000_000.0},
            said={"quantity": 10.0, "unit": "TONNE", "price": 4_000_000.0},
            text="10 tonnes de mais a 4000000",
        )
        assert not result.validation.is_valid
        assert result.question is not None


class TestGoldenDQuestionContext:
    def test_a_bare_reply_to_price_per_tonne_question_is_question_context_explicit(self):
        from ladini.domain.commercial_offer_flow import CommercialQuestion

        question = CommercialQuestion(requested_field="price", expected_basis_unit="TONNE")
        result = _offer(
            {**BASE_PAYLOAD, "price": 450000.0},
            said={"price": 450000.0},
            text="450000",
            question=question,
        )
        assert result.validation.is_valid
        assert result.offer.pricing.basis.value == "PER_BASE_UNIT"
        assert result.offer.pricing.basis_source.value == "QUESTION_CONTEXT_EXPLICIT"


# =====================================================================
# `ProcurementDraft.render_summary`/`execution_payload` - derivation depuis l'offre certifiee
# =====================================================================


class TestProcurementDraftDerivesFromTheCertifiedOffer:
    def test_per_base_unit_offer_shows_the_cap_and_the_corresponding_budget(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 450000.0, "price_unit": "TONNE"},
            said={"quantity": 10.0, "unit": "TONNE", "price": 450000.0, "price_unit": "TONNE"},
            text="10 tonnes de mais a maximum 450000 la tonne",
        )
        assert result.validation.is_valid
        draft = ProcurementDraft.new(
            "d1", product="mais", quantity=10.0, unit="TONNE", price=450000.0,
            commercial_offer=result.offer.to_dict(),
        )
        summary = draft.render_summary()
        assert "Prix plafond : 450 000 FCFA par tonne" in summary
        assert "Budget maximal correspondant : 4 500 000 FCFA" in summary
        payload = draft.execution_payload()
        # Meme convention que `declare_crop_cycle` (B2c.3) : quantite/prix normalises en unite de
        # BASE (TONNE -> KG) - le plafond PAR UNITE (450 FCFA/KG) reste mathematiquement
        # 450 000 FCFA/TONNE, jamais recalcule en 4.5M.
        assert payload["price"] == pytest.approx(450.0)
        assert payload["unit"] == "KG"
        assert payload["quantity"] == pytest.approx(10000.0)

    def test_total_lot_offer_never_multiplies_the_budget_by_the_quantity(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 4_000_000.0},
            said={"quantity": 10.0, "unit": "TONNE", "price": 4_000_000.0},
            text="10 tonnes de mais pour 4 millions au total",
        )
        draft = ProcurementDraft.new(
            "d2", product="mais", quantity=10.0, unit="TONNE", price=4_000_000.0,
            commercial_offer=result.offer.to_dict(),
        )
        summary = draft.render_summary()
        assert "Budget maximal total : 4 000 000 FCFA pour l'ensemble" in summary
        payload = draft.execution_payload()
        # Ecrit dans la colonne existante (pas de migration cette phase) comme derive NORMALISE
        # par unite de base (4M / 10000 kg = 400 FCFA/KG) - jamais le budget brut (4 000 000)
        # reinterprete comme un prix/unite (ce qui aurait donne 4 000 000/TONNE).
        assert payload["price"] == pytest.approx(400.0)
        assert payload["price"] != pytest.approx(4_000_000.0)

    def test_per_package_is_refused_fail_closed_auction_has_no_package_support(self):
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
            commercial_quantity=CommercialQuantity(100.0, "SACHET", Provenance.USER_EXPLICIT),
            inventory_quantity=InventoryQuantity(50.0, "KG", Provenance.USER_EXPLICIT),
            pricing=Pricing(
                amount=500.0, basis=PriceBasis.PER_PACKAGE,
                source=Provenance.USER_EXPLICIT, basis_source=Provenance.USER_EXPLICIT,
            ),
            package=PackageDefinition(
                package_type="SACHET", content_amount=0.5, content_unit="KG",
                status=PackageStatus.KNOWN, source=Provenance.USER_EXPLICIT,
            ),
        )
        assert offer.validate().is_valid  # l'offre elle-meme est valide...
        draft = ProcurementDraft.new(
            "d3", product="tomates", quantity=100.0, unit="SACHET", price=500.0,
            commercial_offer=offer.to_dict(),
        )
        with pytest.raises(ValueError, match="conditionnement"):
            draft.execution_payload()

    def test_an_invalid_offer_is_never_complete(self):
        result = _offer(
            {**BASE_PAYLOAD, "price": 4_000_000.0},
            said={"quantity": 10.0, "unit": "TONNE", "price": 4_000_000.0},
            text="10 tonnes de mais a 4000000",
        )
        assert not result.validation.is_valid
        draft = ProcurementDraft.new(
            "d4", product="mais", quantity=10.0, unit="TONNE", price=4_000_000.0,
            commercial_offer=result.offer.to_dict(),
        )
        assert not draft.is_complete()

    def test_legacy_fallback_without_a_commercial_offer_keeps_the_pre_b2c5_behavior(self):
        draft = ProcurementDraft.new(
            "d5", product="mais", quantity=10.0, unit="TONNE", price=450000.0,
        )
        summary = draft.render_summary()
        assert "au prix plafond de 450 000 FCFA/TONNE" in summary
        payload = draft.execution_payload()
        assert payload["price"] == pytest.approx(450000.0)
        assert payload.get("commercial_offer") is None
