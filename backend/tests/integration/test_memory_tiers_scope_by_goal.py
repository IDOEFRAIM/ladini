"""Portée du changement `memory_update` / `pricing_tiers` (hardening 2026-09-29).

Le correctif « 60 l de miel » retire la dérivation de `price` (1er palier) et de `quantity` (somme des
contenances) — mais UNIQUEMENT pour SALES_PUBLISH_PRODUCT, seul goal qui porte les paliers de bout en bout.
`PRODUCTION_DECLARE_FUTURE` ne supporte PAS officiellement les paliers (`MarketOffer` n'a ni colonne de
paliers ni conditionnement : voir `domain/agro.py::declare_crop_cycle`, fail-closed sur PER_PACKAGE) : son
comportement historique ne doit pas changer en silence à cause d'un chantier SALES_PUBLISH_PRODUCT.
"""
from __future__ import annotations

import pytest

from tests.harness import new_task
from tests.integration.test_sales_publish_packaging_tiers import (  # noqa: F401  (fixture `conv`)
    B5,
    B9,
    INTENT,
    _payload,
    conv,
)

pytestmark = pytest.mark.integration


class TestSalesPublishProductNeverDerivesFromTiers:
    def test_no_price_and_no_summed_quantity_reach_the_payload(self, conv):
        conv.send("Je veux vendre mon miel", llm=new_task(INTENT, product="miel", pricing_tiers=[B5, B9]))
        tp = _payload(conv)
        assert tp.get("price") in (None, ""), "le prix du 1er palier n'est pas un prix"
        assert tp.get("quantity") in (None, ""), "5 L + 9 L ne sont pas un stock de 14 L"


class TestOtherGoalsKeepTheirHistoricalBehaviour:
    def test_production_declare_future_is_not_silently_changed(self, conv):
        conv.send(
            "Je déclare ma future récolte de miel",
            llm=new_task("PRODUCTION_DECLARE_FUTURE", product="miel", pricing_tiers=[B5, B9]),
        )
        tp = _payload(conv)
        # comportement historique (non modifié par ce chantier) : représentant = 1er palier / somme
        assert tp.get("price") == 700.0
        assert tp.get("quantity") == 14.0
