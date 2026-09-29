"""Producer-selection menu (SELECT_PRODUCER) shows certified pricing, not a raw price/unit guess
(Phase B2c.4).

`build_selection_context` (domain/selection_actions.py) builds the "1. Tomates — Coop A —
X FCFA/tonne" menu directly from `vendor_selection_context.vendors`, the same dicts
`search_products` (services/database/buyer.py) produces. Before this phase it always
reconstructed `f"{price} FCFA/{unit}"`, which silently lies for a TOTAL_LOT or packaged offer.
"""
from __future__ import annotations

from ladini.graphs.agents.market_coach.domain.selection_actions import (
    ActionType,
    build_selection_context,
)


def _state_with_vendors(vendors: list) -> dict:
    return {"vendor_selection_context": {"vendors": vendors}}


class TestProducerOptionLabelUsesCertifiedPricing:
    def test_total_lot_offer_shows_its_own_label_not_a_reconstructed_per_unit_price(self):
        vendors = [
            {
                "vendor_name": "Coop A", "producer_id": "prod-a", "price": 4_000_000,
                "unit": "KG", "pricing_label": "4 000 000 FCFA pour l'ensemble",
            },
            {
                "vendor_name": "Coop B", "producer_id": "prod-b", "price": 450000,
                "unit": "TONNE", "pricing_label": "450 000 FCFA par tonne",
            },
        ]
        ctx = build_selection_context(_state_with_vendors(vendors))
        assert ctx.expected_action == ActionType.SELECT_PRODUCER
        labels = [o.label for o in ctx.producer_options]
        assert any("pour l'ensemble" in label for label in labels)
        assert not any("4000000 FCFA/KG" in label.replace(" ", "") for label in labels)

    def test_missing_pricing_label_falls_back_to_the_legacy_raw_format(self):
        vendors = [
            {"vendor_name": "Coop A", "producer_id": "prod-a", "price": 250, "unit": "KG"},
            {"vendor_name": "Coop B", "producer_id": "prod-b", "price": 300, "unit": "KG"},
        ]
        ctx = build_selection_context(_state_with_vendors(vendors))
        assert any("250 FCFA/KG" in o.label for o in ctx.producer_options)
