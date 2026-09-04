"""SALES_UPDATE_PRODUCT + pricing_tiers (2026-08-30).

Before this, `SALES_UPDATE_PRODUCT` was entirely tier-blind end to end —
`SalesUpdateProductCommand` had no `pricing_tiers` field at all, so a
producer had no way to add/edit/remove tiers on an already-published
product. These tests lock the DTO -> Command -> DomainResult chain.
"""
from __future__ import annotations

import pytest

from agriconnect.graphs.agents.market_coach.actions.sales_dto import (
    SalesUpdateProductPayload,
)
from agriconnect.graphs.agents.market_coach.actions.tooling import ToolId
from agriconnect.graphs.agents.market_coach.domain.model import DomainContext
from agriconnect.graphs.agents.market_coach.domain.sales import (
    SalesService,
    SalesUpdateProductCommand,
)


def _context() -> DomainContext:
    return DomainContext(
        user_id="u1",
        phone="+22600000000",
        role="PRODUCER",
        language="fr",
        region=None,
        organization=None,
        permissions=frozenset(),
        tenant=None,
        timezone=None,
    )


class TestSalesUpdateProductPayloadSourcesTiers:
    def test_pricing_tiers_is_sourced_from_payload(self):
        raw_tiers = [{"quantity": 5, "unit": "L", "price": 500, "packaging": "bidon"}]
        dto = SalesUpdateProductPayload.from_state_and_payload(
            payload={"product_id": "p1", "pricing_tiers": raw_tiers},
            entity={},
        )
        assert dto.pricing_tiers == raw_tiers

    def test_pricing_tiers_defaults_to_none_when_absent(self):
        dto = SalesUpdateProductPayload.from_state_and_payload(
            payload={"product_id": "p1", "price": 500}, entity={}
        )
        assert dto.pricing_tiers is None

    def test_empty_list_is_treated_as_absent(self):
        dto = SalesUpdateProductPayload.from_state_and_payload(
            payload={"product_id": "p1", "pricing_tiers": []}, entity={}
        )
        assert dto.pricing_tiers is None


class TestSalesServiceUpdateProductWithTiers:
    def test_pricing_tiers_flow_into_tool_args(self):
        raw_tiers = [{"quantity": 5, "unit": "L", "price": 500, "packaging": "bidon"}]
        command = SalesUpdateProductCommand(
            producer_id="+22600000000",
            product_id="p1",
            pricing_tiers=raw_tiers,
        )
        result = SalesService(context=_context()).update_product(command)
        assert result.tool_id == ToolId.UPDATE_PRODUCT_PRICE_AND_QTY
        assert result.tool_args["pricing_tiers"] == raw_tiers
        assert result.tool_args["product_id"] == "p1"

    def test_pricing_tiers_alone_is_a_sufficient_update(self):
        """Avant, updater UNIQUEMENT les tarifs (sans toucher prix/quantité/
        nom/unité) était rejeté par le garde 'au moins un champ' — les
        tarifs n'étaient tout simplement pas un champ reconnu."""
        command = SalesUpdateProductCommand(
            producer_id="+22600000000",
            product_id="p1",
            pricing_tiers=[{"quantity": 5, "unit": "L", "price": 500}],
        )
        result = SalesService(context=_context()).update_product(command)
        assert "pricing_tiers" in result.tool_args

    def test_no_fields_at_all_still_raises(self):
        command = SalesUpdateProductCommand(
            producer_id="+22600000000", product_id="p1"
        )
        with pytest.raises(ValueError, match="au moins un champ"):
            SalesService(context=_context()).update_product(command)

    def test_scalar_only_update_unaffected_by_tiers_field(self):
        """Régression : une mise à jour prix/quantité classique (sans
        tarifs) ne doit JAMAIS envoyer une clé `pricing_tiers` dans les
        tool_args — sinon le DB tool écraserait les tarifs existants à
        `None` sur une simple correction de prix."""
        command = SalesUpdateProductCommand(
            producer_id="+22600000000", product_id="p1", price=600.0
        )
        result = SalesService(context=_context()).update_product(command)
        assert "pricing_tiers" not in result.tool_args
