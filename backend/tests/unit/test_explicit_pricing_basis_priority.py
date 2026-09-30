"""Priorité du pricing explicite sur le contexte de question (Étape 4, 2026-09-30).

Incident réel :

    Agent: "Quel est votre prix par litre ?"
    User:  "le pot de 4 litre coute 2750 fcfa"
    Résultat AVANT ce correctif : 2750 FCFA/L (basis=PER_BASE_UNIT).

Root cause CONFIRMÉE par audit direct : `package_word_before_amount`/
`price_unit_next_to_amount` (`domain/commercial_offer_flow.py`) ne
reconnaissent un conditionnement AVANT/APRÈS le montant QUE via
`_PACKAGE_WORD_RE`, bâtie sur `_TIER_PACKAGING_WORDS` — la même liste fermée
de 9 mots qu'à l'Étape 3. "pot" n'y figure pas ; la résolution retombe alors
sur `question.expected_basis_unit` (le contexte de la question).

Ce fichier verrouille la nouvelle primitive `parse_generic_package_price_
context` (aucune whitelist) ET son câblage dans `build_commercial_offer_
from_sales_state` (la hiérarchie de priorité réelle).
"""
from __future__ import annotations

import pytest

from ladini.domain.commercial_offer import PriceBasis, Provenance
from ladini.domain.commercial_offer_flow import (
    CommercialQuestion,
    build_commercial_offer_from_sales_state,
    parse_generic_package_price_context,
)

_QUESTION_PRICE_PER_LITRE = CommercialQuestion(
    requested_field="price", expected_basis_unit="LITRE"
)


def _offer(text: str, price: float, *, question=None, unit: str = "LITRE", previous_offer=None):
    payload = {"product": "miel", "price": price, "unit": unit}
    if previous_offer is not None:
        payload["commercial_offer"] = previous_offer
    offer, events = build_commercial_offer_from_sales_state(
        payload, said={"price": price}, text=text, question=question
    )
    return offer, events


# =====================================================================
# Primitive isolée
# =====================================================================


class TestGenericPackagePriceContextPrimitive:
    def test_pot_absent_from_any_whitelist(self):
        from ladini.domain.quantity_unit import _TIER_PACKAGING_WORDS

        assert "pot" not in _TIER_PACKAGING_WORDS

    def test_label_before_amount(self):
        assert parse_generic_package_price_context(
            "le pot de 4 litre coute 2750 fcfa", 2750.0
        ) == ("POT", 4.0, "LITRE")

    def test_label_after_amount(self):
        assert parse_generic_package_price_context("2750 le pot de 4 L", 2750.0) == (
            "POT",
            4.0,
            "LITRE",
        )

    def test_no_size_abstains(self):
        """Limite assumée (voir docstring de section dans commercial_offer_flow.py) :
        un label libre SANS taille n'a aucun signal structurel fiable — jamais deviné."""
        assert parse_generic_package_price_context("500 le cageot", 500.0) is None

    def test_per_base_unit_phrasing_does_not_false_positive(self):
        assert parse_generic_package_price_context("500 FCFA par litre", 500.0) is None


# =====================================================================
# §13 du mandat — cas 1-12
# =====================================================================


class TestPerBaseUnit:
    def test_1_500_fcfa_par_l(self):
        offer, _ = _offer("500 FCFA/L", 500.0)
        assert offer.pricing.basis == PriceBasis.PER_BASE_UNIT
        assert offer.pricing.amount == 500.0

    def test_2_500_le_litre(self):
        offer, _ = _offer("500 le litre", 500.0)
        assert offer.pricing.basis == PriceBasis.PER_BASE_UNIT

    def test_3_bare_500_with_question_context(self):
        offer, _ = _offer("500", 500.0, question=_QUESTION_PRICE_PER_LITRE)
        assert offer.pricing.basis == PriceBasis.PER_BASE_UNIT
        assert offer.pricing.basis_source == Provenance.QUESTION_CONTEXT_EXPLICIT


class TestPerPackage:
    def test_4_question_context_litre_2750_le_pot_de_4L(self):
        offer, _ = _offer(
            "2750 le pot de 4 L", 2750.0, question=_QUESTION_PRICE_PER_LITRE
        )
        assert offer.pricing.basis == PriceBasis.PER_PACKAGE
        assert offer.pricing.basis_source == Provenance.USER_EXPLICIT
        assert offer.package.package_type == "POT"
        assert offer.package.content_amount == 4.0
        assert offer.package.content_unit == "LITRE"

    def test_5_question_context_litre_le_pot_de_4L_coute_2750(self):
        offer, _ = _offer(
            "le pot de 4 L coute 2750", 2750.0, question=_QUESTION_PRICE_PER_LITRE
        )
        assert offer.pricing.basis == PriceBasis.PER_PACKAGE
        assert offer.package.content_amount == 4.0
        assert offer.package.content_unit == "LITRE"
        # Jamais PER_BASE_UNIT/2750 par L — l'interdiction centrale du mandat.
        assert offer.pricing.basis != PriceBasis.PER_BASE_UNIT

    def test_6_500_le_sachet_size_unknown(self):
        offer, _ = _offer("500 le sachet", 500.0)
        assert offer.pricing.basis == PriceBasis.PER_PACKAGE
        assert offer.package.package_type == "SACHET"
        assert offer.package.content_amount is None
        assert offer.package.is_content_known is False
        # Jamais PER_BASE_UNIT en repli.
        assert offer.pricing.basis != PriceBasis.PER_BASE_UNIT

    def test_7_500_le_bidon_de_5L(self):
        offer, _ = _offer("500 le bidon de 5 L", 500.0)
        assert offer.pricing.basis == PriceBasis.PER_PACKAGE
        assert offer.package.package_type == "BIDON"
        assert offer.package.content_amount == 5.0

    @pytest.mark.parametrize(
        "text,label,size,unit_",
        [
            ("cageot de 15kg a 5000", "CAGEOT", 15.0, "KG"),
            ("barquette de 500g a 800", "BARQUETTE", 0.5, "KG"),
            ("3000 la caisse de 12kg", "CAISSE", 12.0, "KG"),
        ],
    )
    def test_10_11_free_label_per_package(self, text, label, size, unit_):
        price = 5000.0 if "5000" in text else (800.0 if "800" in text else 3000.0)
        offer, _ = _offer(text, price)
        assert offer.pricing.basis == PriceBasis.PER_PACKAGE
        assert offer.package.package_type == label
        assert offer.package.content_amount == size
        assert offer.package.content_unit == unit_


class TestMemoryVsExplicitCurrentTurn:
    def test_12_memory_per_package_explicit_current_turn_wins(self):
        previous_offer = {
            "schema": 1,
            "product": "miel",
            "commercial_quantity": None,
            "inventory_quantity": None,
            "pricing": {
                "amount": 2750.0,
                "basis": "PER_PACKAGE",
                "basis_unit": None,
                "currency": "FCFA",
                "source": "USER_EXPLICIT",
                "basis_source": "USER_EXPLICIT",
            },
            "package": {
                "package_type": "POT",
                "content_amount": 4.0,
                "content_unit": "LITRE",
                "status": "KNOWN",
                "source": "USER_EXPLICIT",
            },
            "normalized": None,
        }
        offer, _ = _offer("500 FCFA par litre", 500.0, previous_offer=previous_offer)
        assert offer.pricing.basis == PriceBasis.PER_BASE_UNIT
        assert offer.pricing.basis_source == Provenance.USER_EXPLICIT


# =====================================================================
# §17 du mandat — invariants explicites
# =====================================================================


class TestInvariants:
    def test_i1_explicit_package_pricing_never_per_base_unit(self):
        for text, price in [
            ("le pot de 4 L coute 2750", 2750.0),
            ("500 le bidon de 5 L", 500.0),
            ("cageot de 15kg a 5000", 5000.0),
        ]:
            offer, _ = _offer(text, price, question=_QUESTION_PRICE_PER_LITRE)
            assert offer.pricing.basis != PriceBasis.PER_BASE_UNIT, text

    def test_i2_explicit_base_unit_pricing_is_per_base_unit(self):
        offer, _ = _offer("500 FCFA par litre", 500.0)
        assert offer.pricing.basis == PriceBasis.PER_BASE_UNIT

    def test_i3_explicit_current_turn_beats_question_context(self):
        offer, _ = _offer(
            "2750 le pot de 4 L", 2750.0, question=_QUESTION_PRICE_PER_LITRE
        )
        assert offer.pricing.basis_source == Provenance.USER_EXPLICIT
        assert offer.pricing.basis == PriceBasis.PER_PACKAGE

    def test_i4_explicit_current_turn_beats_memory(self):
        previous_offer = {
            "schema": 1,
            "product": "miel",
            "commercial_quantity": None,
            "inventory_quantity": None,
            "pricing": {
                "amount": 2750.0,
                "basis": "PER_PACKAGE",
                "basis_unit": None,
                "currency": "FCFA",
                "source": "USER_EXPLICIT",
                "basis_source": "USER_EXPLICIT",
            },
            "package": {
                "package_type": "POT",
                "content_amount": 4.0,
                "content_unit": "LITRE",
                "status": "KNOWN",
                "source": "USER_EXPLICIT",
            },
            "normalized": None,
        }
        offer, _ = _offer("500 FCFA par litre", 500.0, previous_offer=previous_offer)
        assert offer.pricing.basis == PriceBasis.PER_BASE_UNIT

    def test_i6_unknown_package_size_is_clarification_not_invention(self):
        offer, _ = _offer("500 le sachet", 500.0)
        assert offer.package.is_content_known is False
        verdict = offer.validate()
        assert verdict.status == "INCOMPLETE"
        assert "package_content_amount" in verdict.missing_fields
