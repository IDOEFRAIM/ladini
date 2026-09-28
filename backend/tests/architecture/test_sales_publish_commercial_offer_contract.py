"""Contrat d'architecture Phase B1 : l'offre commerciale gouverne confirmation ET exécution.

Trois garanties, testées sans dépendre d'un scénario conversationnel :

1. une offre INCOMPLETE ne rend JAMAIS un draft « complet » (donc jamais WAITING_CONFIRMATION) ;
2. `execution_payload()` est DÉRIVÉ de l'offre certifiée — les champs legacy du draft ne décident
   plus de rien (un prix legacy « 1 » ne peut pas contredire l'offre) ;
3. le récapitulatif de confirmation est une projection du draft, pas du payload brut."""
from __future__ import annotations

import inspect

import pytest

from ladini.domain.commercial_offer_flow import evaluate_sales_offer
from ladini.graphs.agents.market_coach.domain.sales_publish_draft import (
    SalesPublishDraft,
)
from ladini.graphs.agents.market_coach.nodes.rendering import (
    confirm as confirm_renderer,
)


def _offer(payload, *, said, text, question=None):
    return evaluate_sales_offer(payload, said=said, text=text, question=question)


def _sachet_offer_without_content():
    return _offer(
        {"product": "lait", "quantity": 50.0, "unit": "LITRE", "price": 500.0, "price_unit": "SAC"},
        said={"price": 500.0, "price_unit": "SAC"},
        text="500f le sachet",
    )


def _draft(offer, **legacy):
    fields = {"product": "lait", "quantity": 50.0, "unit": "LITRE", "price": 500.0, **legacy}
    return SalesPublishDraft.new("draft-b1", commercial_offer=offer.to_dict(), **fields)


class TestAnIncompleteOfferNeverReachesConfirmation:
    def test_draft_with_unknown_package_size_is_not_complete(self):
        draft = _draft(_sachet_offer_without_content().offer)
        assert not draft.is_complete()
        assert "package_size" in draft.missing_fields()

    def test_draft_with_unknown_price_basis_is_not_complete(self):
        result = _offer(
            {"product": "maïs", "quantity": 200.0, "unit": "TONNE", "price": 500000.0},
            said={"quantity": 200.0, "unit": "TONNE", "price": 500000.0},
            text="200 tonnes de maïs à 500000",
        )
        draft = SalesPublishDraft.new(
            "draft-b1", product="maïs", quantity=200.0, unit="TONNE", price=500000.0,
            commercial_offer=result.offer.to_dict(),
        )
        assert not draft.is_complete()

    def test_execution_payload_refuses_an_incomplete_offer(self):
        draft = _draft(_sachet_offer_without_content().offer)
        with pytest.raises(ValueError):
            draft.execution_payload()


class TestExecutionPayloadIsDerivedFromTheCertifiedOffer:
    def test_legacy_fields_cannot_contradict_the_offer(self):
        first = _sachet_offer_without_content()
        valid = _offer(
            {
                "product": "lait", "quantity": 50.0, "unit": "LITRE", "price": 500.0, "price_unit": "SAC",
                "commercial_offer": first.offer.to_dict(),
            },
            said={},
            text="0,5 litre",
            question=first.question,
        )
        assert valid.validation.status == "VALID"
        # champs legacy volontairement FAUX : l'exécution ne doit lire que l'offre
        draft = _draft(valid.offer, quantity=999.0, price=1.0)
        payload = draft.execution_payload()
        assert (payload["quantity"], payload["unit"]) == (50.0, "LITRE")
        assert payload["price"] == 1000.0
        assert payload["pricing_tiers"] == [
            {"quantity": 0.5, "unit": "LITRE", "price": 500.0, "packaging": "sachet"}
        ]

    def test_summary_is_the_commercial_sentence_of_the_offer(self):
        first = _sachet_offer_without_content()
        valid = _offer(
            {
                "product": "lait", "quantity": 50.0, "unit": "LITRE", "price": 500.0, "price_unit": "SAC",
                "commercial_offer": first.offer.to_dict(),
            },
            said={},
            text="0,5 litre",
            question=first.question,
        )
        assert "500 FCFA par sachet de 0,5 litre" in _draft(valid.offer).render_summary()


class TestTheConfirmationRendererProjectsTheDraft:
    def test_render_confirmation_reads_render_summary_of_the_sales_draft(self):
        source = inspect.getsource(confirm_renderer)
        assert "render_summary" in source and "sales_publish_draft" in source
